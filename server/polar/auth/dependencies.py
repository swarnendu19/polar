from collections.abc import Awaitable, Callable
from inspect import Parameter, Signature
from typing import Annotated, Any

from fastapi import Depends, Request, Security
from makefun import with_signature

from polar.auth.scope import RESERVED_SCOPES, Scope
from polar.customer_session.dependencies import get_optional_customer_session_token
from polar.exceptions import NotPermitted, Unauthorized
from polar.models import (
    Customer,
    CustomerSession,
    OAuth2Token,
    OrganizationAccessToken,
    PersonalAccessToken,
    UserSession,
)
from polar.oauth2.dependencies import get_optional_token
from polar.oauth2.exceptions import InsufficientScopeError, InvalidTokenError
from polar.organization_access_token.dependencies import (
    get_optional_organization_access_token,
)
from polar.personal_access_token.dependencies import get_optional_personal_access_token
from polar.postgres import AsyncSession, get_db_session
from polar.sentry import set_sentry_user

from .models import (
    Anonymous,
    AuthMethod,
    AuthSubject,
    Organization,
    Subject,
    SubjectType,
    User,
    is_anonymous,
)
from .service import auth as auth_service


async def get_user_session(
    request: Request, session: AsyncSession = Depends(get_db_session)
) -> UserSession | None:
    return await auth_service.authenticate(session, request)


async def _get_auth_subject(
    customer_session_credentials: tuple[CustomerSession | None, bool] = (None, False),
    user_session: UserSession | None = None,
    oauth2_credentials: tuple[OAuth2Token | None, bool] = (None, False),
    personal_access_token_credentials: tuple[PersonalAccessToken | None, bool] = (
        None,
        False,
    ),
    organization_access_token_credentials: tuple[
        OrganizationAccessToken | None, bool
    ] = (None, False),
) -> AuthSubject[Subject]:
    # Customer session is prioritized over web session
    customer_session, customer_session_authorization_set = customer_session_credentials
    if customer_session:
        return AuthSubject(
            customer_session.customer,
            {Scope.customer_portal_write},
            AuthMethod.CUSTOMER_SESSION_TOKEN,
        )

    # Web session
    if user_session is not None:
        return AuthSubject(user_session.user, {Scope.web_default}, AuthMethod.COOKIE)

    oauth2_token, oauth2_authorization_set = oauth2_credentials
    personal_access_token, personal_access_token_authorization_set = (
        personal_access_token_credentials
    )
    organization_access_token, organization_access_token_authorization_set = (
        organization_access_token_credentials
    )

    if oauth2_token:
        return AuthSubject(
            oauth2_token.sub, oauth2_token.scopes, AuthMethod.OAUTH2_ACCESS_TOKEN
        )

    if personal_access_token:
        return AuthSubject(
            personal_access_token.user,
            personal_access_token.scopes,
            AuthMethod.PERSONAL_ACCESS_TOKEN,
        )

    if organization_access_token:
        return AuthSubject(
            organization_access_token.organization,
            organization_access_token.scopes,
            AuthMethod.ORGANIZATION_ACCESS_TOKEN,
        )

    if any(
        (
            customer_session_authorization_set,
            oauth2_authorization_set,
            personal_access_token_authorization_set,
            organization_access_token_authorization_set,
        )
    ):
        raise InvalidTokenError()

    return AuthSubject(Anonymous(), set(), AuthMethod.NONE)


_auth_subject_factory_cache: dict[
    frozenset[SubjectType], Callable[..., Awaitable[AuthSubject[Subject]]]
] = {}


def _get_auth_subject_factory(
    allowed_subjects: frozenset[SubjectType],
) -> Callable[..., Awaitable[AuthSubject[Subject]]]:
    if allowed_subjects in _auth_subject_factory_cache:
        return _auth_subject_factory_cache[allowed_subjects]

    parameters: list[Parameter] = []
    if User in allowed_subjects or Organization in allowed_subjects:
        parameters += [
            Parameter(
                name="oauth2_credentials",
                kind=Parameter.KEYWORD_ONLY,
                default=Depends(get_optional_token),
            )
        ]
    if User in allowed_subjects:
        parameters += [
            Parameter(
                name="user_session",
                kind=Parameter.KEYWORD_ONLY,
                default=Depends(get_user_session),
            ),
            Parameter(
                name="personal_access_token_credentials",
                kind=Parameter.KEYWORD_ONLY,
                default=Depends(get_optional_personal_access_token),
            ),
        ]
    if Organization in allowed_subjects:
        parameters += [
            Parameter(
                name="organization_access_token_credentials",
                kind=Parameter.KEYWORD_ONLY,
                default=Depends(get_optional_organization_access_token),
            )
        ]
    if Customer in allowed_subjects:
        parameters.append(
            Parameter(
                name="customer_session_credentials",
                kind=Parameter.KEYWORD_ONLY,
                default=Depends(get_optional_customer_session_token),
            )
        )

    signature = Signature(parameters)

    @with_signature(signature)
    async def get_auth_subject(**kwargs: Any) -> AuthSubject[Subject]:
        return await _get_auth_subject(**kwargs)

    _auth_subject_factory_cache[allowed_subjects] = get_auth_subject

    return get_auth_subject


class _Authenticator:
    def __init__(
        self,
        *,
        allowed_subjects: frozenset[SubjectType],
        required_scopes: set[Scope] | None = None,
    ) -> None:
        self.allowed_subjects = allowed_subjects
        self.required_scopes = required_scopes

    async def __call__(
        self, auth_subject: AuthSubject[Subject]
    ) -> AuthSubject[Subject]:
        # Anonymous
        if is_anonymous(auth_subject):
            if Anonymous in self.allowed_subjects:
                return auth_subject
            else:
                raise Unauthorized()

        set_sentry_user(auth_subject)

        # Blocked subjects
        blocked_at = getattr(auth_subject.subject, "blocked_at", None)
        if blocked_at is not None:
            raise NotPermitted()

        # Not allowed subject
        subject_type = type(auth_subject.subject)
        if subject_type not in self.allowed_subjects:
            raise InvalidTokenError(
                "The subject of this access token is not valid for this endpoint.",
                allowed_subjects=" ".join(s.__name__ for s in self.allowed_subjects),
            )

        # No required scopes
        if not self.required_scopes:
            return auth_subject

        # Have at least one of the required scopes. Allow this request.
        if auth_subject.scopes & self.required_scopes:
            return auth_subject

        raise InsufficientScopeError({s for s in self.required_scopes})


def Authenticator(
    allowed_subjects: set[SubjectType],
    required_scopes: set[Scope] | None = None,
) -> _Authenticator:
    """
    Here comes some blood magic 🧙‍♂️

    Generate a version of `_Authenticator` with an overriden `__call__` signature.

    By doing so, we can dynamically inject the required scopes into FastAPI
    dependency, so they are properrly detected by the OpenAPI generator.
    """
    allowed_subjects_frozen = frozenset(allowed_subjects)

    parameters: list[Parameter] = [
        Parameter(name="self", kind=Parameter.POSITIONAL_OR_KEYWORD),
        Parameter(
            name="auth_subject",
            kind=Parameter.POSITIONAL_OR_KEYWORD,
            default=Security(
                _get_auth_subject_factory(allowed_subjects_frozen),
                scopes=sorted(
                    [
                        s.value
                        for s in (required_scopes or {})
                        if s not in RESERVED_SCOPES
                    ]
                ),
            ),
        ),
    ]
    signature = Signature(parameters)

    class _AuthenticatorSignature(_Authenticator):
        @with_signature(signature)
        async def __call__(
            self, auth_subject: AuthSubject[Subject]
        ) -> AuthSubject[Subject]:
            return await super().__call__(auth_subject)

    return _AuthenticatorSignature(
        allowed_subjects=allowed_subjects_frozen, required_scopes=required_scopes
    )


_WebUserOrAnonymous = Authenticator(
    allowed_subjects={Anonymous, User}, required_scopes={Scope.web_default}
)
WebUserOrAnonymous = Annotated[
    AuthSubject[Anonymous | User], Depends(_WebUserOrAnonymous)
]

_WebUser = Authenticator(allowed_subjects={User}, required_scopes={Scope.web_default})
WebUser = Annotated[AuthSubject[User], Depends(_WebUser)]

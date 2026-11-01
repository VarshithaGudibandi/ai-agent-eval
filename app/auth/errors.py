class AuthError(Exception):
    pass


class SetupAlreadyDoneError(AuthError):
    pass


class InvalidCredentialsError(AuthError):
    pass


class NotAMemberError(AuthError):
    pass


class InviteInvalidError(AuthError):
    pass


class RoleTooHighError(AuthError):
    pass


class UserRequiredError(AuthError):
    """Only a signed-in person can do this, not an API token."""

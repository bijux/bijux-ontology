"""Expected failures at owned input and execution boundaries."""


class InputError(ValueError):
    """A supplied value cannot establish the requested input contract."""

    def __init__(self, message: str, *, location: str = "") -> None:
        self.location = location
        super().__init__(f"{location}: {message}" if location else message)


class ResourceError(InputError):
    """An explicitly required resource could not be supplied or decoded."""


class LimitExceeded(InputError):
    """Owned execution stopped at an explicit resource or work bound."""

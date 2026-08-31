"""Exception hierarchy. Every failure the CLI reports derives from TicketError."""


class TicketError(Exception):
    """Base for every error this tool raises deliberately."""


class ConfigError(TicketError):
    """Config file is missing, malformed, or internally inconsistent."""


class StoreError(TicketError):
    """Store is unreadable, locked, or holds something unexpected."""


class StepError(TicketError):
    """A step could not be executed."""


class GhError(TicketError):
    """A gh or git invocation failed after retries."""

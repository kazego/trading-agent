"""Classification contracts only; no retry, recovery, or order actions."""

from dataclasses import dataclass
from enum import Enum


class ErrorAction(str, Enum):
    RETRY_READ = "RETRY_READ"
    REQUIRE_OPERATOR = "REQUIRE_OPERATOR"
    RECONCILE = "RECONCILE"
    PROTECT_POSITION = "PROTECT_POSITION"


@dataclass(frozen=True)
class ErrorClassification:
    recoverable: bool
    action: ErrorAction
    blocks_entries: bool = True


class ApplicationError(Exception):
    """Unclassified application errors default to blocking entries."""


class RecoverableReadError(ApplicationError):
    """Only for reads, never an uncertain order submission."""


class TransientReadError(RecoverableReadError):
    pass


class ReadTimeoutError(RecoverableReadError):
    pass


class RateLimitError(RecoverableReadError):
    pass


class CriticalError(ApplicationError):
    pass


class ConfigurationError(CriticalError):
    pass


class AuthenticationError(CriticalError):
    pass


class AccountModeError(CriticalError):
    pass


class UnknownExchangeStateError(CriticalError):
    pass


class PersistenceUnavailableError(CriticalError):
    pass


class SchemaMismatchError(PersistenceUnavailableError):
    pass


class PersistenceConflictError(CriticalError):
    """A uniqueness, stale-update, or local-state constraint was violated."""


class RuntimeOwnershipError(CriticalError):
    """Another process/store already owns this local database."""


class ProtectionFailureError(CriticalError):
    pass


class LoggingUnavailableError(CriticalError):
    pass


def classify_error(error: Exception) -> ErrorClassification:
    if isinstance(error, RecoverableReadError):
        return ErrorClassification(True, ErrorAction.RETRY_READ)
    if isinstance(error, UnknownExchangeStateError):
        return ErrorClassification(False, ErrorAction.RECONCILE)
    if isinstance(error, ProtectionFailureError):
        return ErrorClassification(False, ErrorAction.PROTECT_POSITION)
    return ErrorClassification(False, ErrorAction.REQUIRE_OPERATOR)

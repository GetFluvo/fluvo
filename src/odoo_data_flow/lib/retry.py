"""Retry logic module for handling transient and recoverable errors.

This module provides intelligent error categorization and retry strategies
for import operations, distinguishing between:
- Transient errors: Temporary issues that may succeed on retry
- Permanent errors: Structural issues that will never succeed
- Recoverable errors: Issues that can be resolved with alternative actions
"""

import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional, TypeVar

from ..logging_config import log

T = TypeVar("T")


class ErrorCategory(Enum):
    """Categories of errors for retry decision making."""

    TRANSIENT = "transient"  # May succeed on retry
    PERMANENT = "permanent"  # Will never succeed
    RECOVERABLE = "recoverable"  # Can be handled with alternative action


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""

    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    exponential_base: float = 2.0
    jitter: bool = True


@dataclass
class RetryStats:
    """Statistics about retry operations."""

    total_attempts: int = 0
    successful_retries: int = 0
    failed_retries: int = 0
    transient_errors: int = 0
    permanent_errors: int = 0
    recoverable_errors: int = 0
    total_retry_delay: float = 0.0
    error_counts: dict[str, int] = field(default_factory=dict)

    def record_error(self, category: ErrorCategory, error_type: str) -> None:
        """Record an error occurrence."""
        if category == ErrorCategory.TRANSIENT:
            self.transient_errors += 1
        elif category == ErrorCategory.PERMANENT:
            self.permanent_errors += 1
        elif category == ErrorCategory.RECOVERABLE:
            self.recoverable_errors += 1

        self.error_counts[error_type] = self.error_counts.get(error_type, 0) + 1


# Error patterns for categorization
TRANSIENT_ERROR_PATTERNS = [
    # Network/connection issues
    "timeout",
    "timed out",
    "read timeout",
    "connection refused",
    "connection reset",
    "connection closed",
    "network unreachable",
    "name resolution failed",
    "dns",
    "broken pipe",
    "connection aborted",
    "remotedisconnected",
    "connectionerror",
    # Server overload
    "502",
    "503",
    "504",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "server busy",
    "too many requests",
    "rate limit",
    # Server crash / empty response (common with single worker)
    "jsondecode",
    "json decode",
    "expecting value",  # JSONDecodeError message
    "empty response",
    "no data",
    "incomplete read",
    "response ended prematurely",
    "eof occurred",
    "unexpected eof",
    # Database contention
    "could not serialize access",
    "concurrent update",
    "deadlock",
    "lock wait timeout",
    "database is locked",
    # Resource exhaustion
    "connection pool",
    "too many connections",
    "poolerror",
    "out of memory",
    "memory",
    # Odoo/server transient
    "bus.bus",
    "cursor already closed",
    "transaction aborted",
    "server closed connection",
    "internal server error",
    "500",
]

PERMANENT_ERROR_PATTERNS = [
    # Constraint violations
    "unique constraint",
    "duplicate key",
    "violates unique",
    "already exists",
    # Field/type errors
    "invalid literal",
    "invalid value",
    "incorrect type",
    "type error",
    "cannot cast",
    # Access/permission errors
    "access denied",
    "permission denied",
    "access rights",
    "not allowed",
    "security restriction",
    # Structure errors
    "field does not exist",
    "unknown field",
    "model does not exist",
    "invalid model",
    "no such column",
    # Validation errors
    "validation error",
    "required field",
    "cannot be empty",
    "invalid format",
]

RECOVERABLE_ERROR_PATTERNS = [
    # Missing references (can try auto-create or skip field)
    "no matching record found",
    "external id",
    "xmlid",
    "missing required value",
    "not found in",
    "reference not found",
    # Company access issues (can adjust context)
    "company",
    "multi-company",
    "allowed_company",
]


def categorize_error(error: str) -> tuple[ErrorCategory, str]:
    """Categorize an error message into transient, permanent, or recoverable.

    Args:
        error: The error message string.

    Returns:
        Tuple of (ErrorCategory, matched_pattern).
    """
    error_lower = error.lower()

    # Check transient patterns first (higher priority)
    for pattern in TRANSIENT_ERROR_PATTERNS:
        if pattern in error_lower:
            return ErrorCategory.TRANSIENT, pattern

    # Check recoverable patterns
    for pattern in RECOVERABLE_ERROR_PATTERNS:
        if pattern in error_lower:
            return ErrorCategory.RECOVERABLE, pattern

    # Check permanent patterns
    for pattern in PERMANENT_ERROR_PATTERNS:
        if pattern in error_lower:
            return ErrorCategory.PERMANENT, pattern

    # Default to permanent for unknown errors (fail fast)
    return ErrorCategory.PERMANENT, "unknown"


def calculate_backoff_delay(
    attempt: int,
    config: RetryConfig,
) -> float:
    """Calculate exponential backoff delay with optional jitter.

    Args:
        attempt: The current retry attempt (1-based).
        config: Retry configuration.

    Returns:
        Delay in seconds before next retry.
    """
    # Exponential backoff: base_delay * (exponential_base ^ attempt)
    delay = config.base_delay * (config.exponential_base ** (attempt - 1))

    # Cap at max delay
    delay = min(delay, config.max_delay)

    # Add jitter to prevent thundering herd
    if config.jitter:
        jitter_range = delay * 0.25
        delay = delay + random.uniform(-jitter_range, jitter_range)  # noqa: S311

    return max(0.1, delay)  # Minimum 100ms


def retry_with_backoff(
    func: Callable[[], T],
    config: Optional[RetryConfig] = None,
    stats: Optional[RetryStats] = None,
    on_retry: Optional[Callable[[int, str, float], None]] = None,
) -> tuple[Optional[T], Optional[str]]:
    """Execute a function with exponential backoff retry.

    Args:
        func: Function to execute.
        config: Retry configuration.
        stats: Stats object to update.
        on_retry: Callback for retry events (attempt, error, delay).

    Returns:
        Tuple of (result, error_message). Result is None if all retries failed.
    """
    config = config or RetryConfig()
    stats = stats or RetryStats()

    last_error = ""
    for attempt in range(1, config.max_retries + 2):  # +2 for initial + retries
        stats.total_attempts += 1

        try:
            result = func()
            if attempt > 1:
                stats.successful_retries += 1
            return result, None

        except Exception as e:
            last_error = str(e)
            category, pattern = categorize_error(last_error)
            stats.record_error(category, pattern)

            # Don't retry permanent errors
            if category == ErrorCategory.PERMANENT:
                log.debug(f"Permanent error (pattern: {pattern}), not retrying: {e}")
                return None, last_error

            # Check if we have retries left
            if attempt > config.max_retries:
                stats.failed_retries += 1
                log.debug(f"Max retries ({config.max_retries}) exceeded: {e}")
                return None, last_error

            # Calculate delay and wait
            delay = calculate_backoff_delay(attempt, config)
            stats.total_retry_delay += delay

            log.debug(
                f"Retry {attempt}/{config.max_retries} after {delay:.2f}s "
                f"(error: {pattern}): {e}"
            )

            if on_retry:
                on_retry(attempt, last_error, delay)

            time.sleep(delay)

    return None, last_error


def should_retry_error(error: str) -> bool:
    """Quick check if an error should be retried.

    Args:
        error: The error message string.

    Returns:
        True if the error is transient and should be retried.
    """
    category, _ = categorize_error(error)
    return category == ErrorCategory.TRANSIENT


def is_recoverable_error(error: str) -> bool:
    """Check if an error is recoverable with alternative action.

    Args:
        error: The error message string.

    Returns:
        True if the error can be recovered with alternative action.
    """
    category, _ = categorize_error(error)
    return category == ErrorCategory.RECOVERABLE


def get_retry_recommendation(error: str) -> dict[str, Any]:
    """Get a recommendation for how to handle an error.

    Args:
        error: The error message string.

    Returns:
        Dictionary with recommendation details.
    """
    category, pattern = categorize_error(error)

    recommendation: dict[str, Any] = {
        "category": category.value,
        "pattern": pattern,
        "should_retry": category == ErrorCategory.TRANSIENT,
        "action": "fail",
    }

    if category == ErrorCategory.TRANSIENT:
        recommendation["action"] = "retry"
        recommendation["message"] = (
            f"Transient error ({pattern}). Will retry with exponential backoff."
        )
    elif category == ErrorCategory.RECOVERABLE:
        if "company" in pattern.lower():
            recommendation["action"] = "adjust_context"
            recommendation["message"] = (
                "Company access issue. Consider using --all-companies flag."
            )
        elif "reference" in pattern.lower() or "not found" in pattern.lower():
            recommendation["action"] = "skip_or_create"
            recommendation["message"] = (
                "Missing reference. Use --on-missing-ref to handle."
            )
        else:
            recommendation["action"] = "investigate"
            recommendation["message"] = (
                f"Recoverable error ({pattern}). May need config adjustment."
            )
    else:
        recommendation["action"] = "fail"
        recommendation["message"] = (
            f"Permanent error ({pattern}). Record will be written to fail file."
        )

    return recommendation

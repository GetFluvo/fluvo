"""Health-aware throttling module for adaptive batch processing.

This module provides functionality to monitor server health and automatically
adjust batch sizes and delays to prevent overloading the Odoo server.
"""

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ..logging_config import log


class ServerHealth(Enum):
    """Server health status levels (ordered by severity)."""

    HEALTHY = 0
    DEGRADED = 1
    STRESSED = 2
    OVERLOADED = 3


@dataclass
class ThrottleConfig:
    """Configuration for throttling behavior."""

    # Response time thresholds (seconds)
    healthy_threshold: float = 2.0  # Below this = healthy
    degraded_threshold: float = 5.0  # Below this = degraded
    stressed_threshold: float = 10.0  # Below this = stressed
    # Above stressed_threshold = overloaded

    # Base delays for each health level (seconds)
    healthy_delay: float = 0.0
    degraded_delay: float = 0.5
    stressed_delay: float = 2.0
    overloaded_delay: float = 5.0

    # Batch size multipliers for each health level
    healthy_batch_multiplier: float = 1.0
    degraded_batch_multiplier: float = 0.75
    stressed_batch_multiplier: float = 0.5
    overloaded_batch_multiplier: float = 0.25

    # Rolling average window for response times
    window_size: int = 5

    # Recovery settings
    recovery_requests: int = 3  # Consecutive fast responses to improve health
    min_batch_size: int = 1


@dataclass
class ThrottleStats:
    """Statistics for throttling operations."""

    total_requests: int = 0
    healthy_requests: int = 0
    degraded_requests: int = 0
    stressed_requests: int = 0
    overloaded_requests: int = 0
    total_delay_added: float = 0.0
    batch_size_reductions: int = 0
    health_recoveries: int = 0
    min_response_time: float = float("inf")
    max_response_time: float = 0.0
    total_response_time: float = 0.0

    @property
    def avg_response_time(self) -> float:
        """Calculate average response time."""
        if self.total_requests == 0:
            return 0.0
        return self.total_response_time / self.total_requests


class ThrottleController:
    """Controller for health-aware throttling."""

    def __init__(self, config: Optional[ThrottleConfig] = None):
        """Initialize the throttle controller.

        Args:
            config: Throttling configuration.
        """
        self.config = config or ThrottleConfig()
        self.stats = ThrottleStats()
        self.response_times: list[float] = []
        self.current_health = ServerHealth.HEALTHY
        self.consecutive_fast_responses = 0
        self.current_delay = 0.0
        self.batch_size_factor = 1.0

    def record_response(self, response_time: float) -> None:
        """Record a response time and update health status.

        Args:
            response_time: Time taken for the request in seconds.
        """
        self.stats.total_requests += 1
        self.stats.total_response_time += response_time
        self.stats.min_response_time = min(
            self.stats.min_response_time, response_time
        )
        self.stats.max_response_time = max(
            self.stats.max_response_time, response_time
        )

        # Add to rolling window
        self.response_times.append(response_time)
        if len(self.response_times) > self.config.window_size:
            self.response_times.pop(0)

        # Update health based on average
        self._update_health()

    def _update_health(self) -> None:
        """Update health status based on rolling average response time."""
        if not self.response_times:
            return

        avg_time = sum(self.response_times) / len(self.response_times)
        old_health = self.current_health

        # Determine new health level
        if avg_time < self.config.healthy_threshold:
            new_health = ServerHealth.HEALTHY
            self.consecutive_fast_responses += 1
        elif avg_time < self.config.degraded_threshold:
            new_health = ServerHealth.DEGRADED
            self.consecutive_fast_responses = 0
        elif avg_time < self.config.stressed_threshold:
            new_health = ServerHealth.STRESSED
            self.consecutive_fast_responses = 0
        else:
            new_health = ServerHealth.OVERLOADED
            self.consecutive_fast_responses = 0

        # Track health level in stats
        if new_health == ServerHealth.HEALTHY:
            self.stats.healthy_requests += 1
        elif new_health == ServerHealth.DEGRADED:
            self.stats.degraded_requests += 1
        elif new_health == ServerHealth.STRESSED:
            self.stats.stressed_requests += 1
        else:
            self.stats.overloaded_requests += 1

        # Update current health (with hysteresis for recovery)
        if new_health.value > old_health.value:
            # Health degraded - update immediately
            self.current_health = new_health
            self._update_throttle_params()
            log.debug(
                f"Server health degraded: {old_health.value} -> {new_health.value}"
            )
        elif (
            new_health.value < old_health.value
            and self.consecutive_fast_responses >= self.config.recovery_requests
        ):
            # Health improved and we have enough consecutive fast responses
            self.current_health = new_health
            self._update_throttle_params()
            self.consecutive_fast_responses = 0
            self.stats.health_recoveries += 1
            log.debug(
                f"Server health recovered: {old_health.value} -> {new_health.value}"
            )

    def _update_throttle_params(self) -> None:
        """Update delay and batch size based on current health."""
        if self.current_health == ServerHealth.HEALTHY:
            self.current_delay = self.config.healthy_delay
            self.batch_size_factor = self.config.healthy_batch_multiplier
        elif self.current_health == ServerHealth.DEGRADED:
            self.current_delay = self.config.degraded_delay
            self.batch_size_factor = self.config.degraded_batch_multiplier
        elif self.current_health == ServerHealth.STRESSED:
            self.current_delay = self.config.stressed_delay
            self.batch_size_factor = self.config.stressed_batch_multiplier
        else:
            self.current_delay = self.config.overloaded_delay
            self.batch_size_factor = self.config.overloaded_batch_multiplier

    def get_delay(self) -> float:
        """Get the recommended delay before next request.

        Returns:
            Delay in seconds.
        """
        return self.current_delay

    def get_batch_size(self, original_batch_size: int) -> int:
        """Get the recommended batch size.

        Args:
            original_batch_size: The original configured batch size.

        Returns:
            Adjusted batch size.
        """
        adjusted = int(original_batch_size * self.batch_size_factor)
        if adjusted < original_batch_size:
            self.stats.batch_size_reductions += 1
        return max(self.config.min_batch_size, adjusted)

    def apply_delay(self) -> None:
        """Apply the current delay (sleep)."""
        if self.current_delay > 0:
            self.stats.total_delay_added += self.current_delay
            time.sleep(self.current_delay)

    def get_health_status(self) -> dict:
        """Get current health status as a dict.

        Returns:
            Dict with health status information.
        """
        return {
            "health": self.current_health,
            "avg_response_time": (
                sum(self.response_times) / len(self.response_times)
                if self.response_times
                else 0
            ),
            "current_delay": self.current_delay,
            "batch_size_factor": self.batch_size_factor,
        }

    def record_error(self, is_server_error: bool = False) -> None:
        """Record an error and adjust throttling if needed.

        Args:
            is_server_error: True if error indicates server overload (5xx).
        """
        if is_server_error:
            # Treat server errors as very slow responses
            self.record_response(self.config.stressed_threshold * 2)
            log.debug("Server error recorded, increasing throttle")


def create_throttle_controller(
    base_delay: float = 0.0,
    aggressive: bool = False,
) -> ThrottleController:
    """Create a throttle controller with preset configurations.

    Args:
        base_delay: Base delay to add to all operations.
        aggressive: If True, use more aggressive throttling.

    Returns:
        Configured ThrottleController.
    """
    if aggressive:
        config = ThrottleConfig(
            healthy_threshold=1.0,
            degraded_threshold=3.0,
            stressed_threshold=5.0,
            healthy_delay=base_delay,
            degraded_delay=base_delay + 1.0,
            stressed_delay=base_delay + 3.0,
            overloaded_delay=base_delay + 10.0,
            healthy_batch_multiplier=1.0,
            degraded_batch_multiplier=0.5,
            stressed_batch_multiplier=0.25,
            overloaded_batch_multiplier=0.1,
        )
    else:
        config = ThrottleConfig(
            healthy_delay=base_delay,
            degraded_delay=base_delay + 0.5,
            stressed_delay=base_delay + 2.0,
            overloaded_delay=base_delay + 5.0,
        )
    return ThrottleController(config)


def display_throttle_stats(stats: ThrottleStats) -> None:
    """Display throttling statistics."""
    from rich.console import Console
    from rich.panel import Panel

    console = Console()

    lines = [
        f"Total requests: {stats.total_requests}",
        f"Avg response time: {stats.avg_response_time:.2f}s",
        f"Min/Max response: {stats.min_response_time:.2f}s / "
        f"{stats.max_response_time:.2f}s",
        "",
        "Health distribution:",
        f"  Healthy: {stats.healthy_requests}",
        f"  Degraded: {stats.degraded_requests}",
        f"  Stressed: {stats.stressed_requests}",
        f"  Overloaded: {stats.overloaded_requests}",
        "",
        f"Total delay added: {stats.total_delay_added:.2f}s",
        f"Batch size reductions: {stats.batch_size_reductions}",
        f"Health recoveries: {stats.health_recoveries}",
    ]

    console.print(
        Panel(
            "\n".join(lines),
            title="[bold cyan]Throttling Statistics[/bold cyan]",
            expand=False,
        )
    )

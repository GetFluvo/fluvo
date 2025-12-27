"""Tests for the health-aware throttling module."""

from odoo_data_flow.lib import throttle


class TestServerHealth:
    """Tests for ServerHealth enum."""

    def test_health_levels(self) -> None:
        """Test that health levels are correctly ordered."""
        assert throttle.ServerHealth.HEALTHY.value == 0
        assert throttle.ServerHealth.DEGRADED.value == 1
        assert throttle.ServerHealth.STRESSED.value == 2
        assert throttle.ServerHealth.OVERLOADED.value == 3
        # Ensure ordering works
        assert (
            throttle.ServerHealth.HEALTHY.value < throttle.ServerHealth.DEGRADED.value
        )
        assert (
            throttle.ServerHealth.DEGRADED.value < throttle.ServerHealth.STRESSED.value
        )


class TestThrottleConfig:
    """Tests for ThrottleConfig dataclass."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        config = throttle.ThrottleConfig()

        assert config.healthy_threshold == 2.0
        assert config.degraded_threshold == 5.0
        assert config.stressed_threshold == 10.0
        assert config.healthy_delay == 0.0
        assert config.window_size == 5

    def test_custom_values(self) -> None:
        """Test custom configuration values."""
        config = throttle.ThrottleConfig(
            healthy_threshold=1.0,
            degraded_delay=1.0,
            window_size=10,
        )

        assert config.healthy_threshold == 1.0
        assert config.degraded_delay == 1.0
        assert config.window_size == 10


class TestThrottleStats:
    """Tests for ThrottleStats dataclass."""

    def test_avg_response_time_no_requests(self) -> None:
        """Test average response time with no requests."""
        stats = throttle.ThrottleStats()
        assert stats.avg_response_time == 0.0

    def test_avg_response_time(self) -> None:
        """Test average response time calculation."""
        stats = throttle.ThrottleStats(
            total_requests=10,
            total_response_time=20.0,
        )
        assert stats.avg_response_time == 2.0


class TestThrottleController:
    """Tests for ThrottleController class."""

    def test_initial_state(self) -> None:
        """Test initial controller state."""
        controller = throttle.ThrottleController()

        assert controller.current_health == throttle.ServerHealth.HEALTHY
        assert controller.current_delay == 0.0
        assert controller.batch_size_factor == 1.0

    def test_healthy_response(self) -> None:
        """Test recording a healthy response."""
        controller = throttle.ThrottleController()
        controller.record_response(1.0)

        assert controller.current_health == throttle.ServerHealth.HEALTHY
        assert controller.stats.healthy_requests == 1

    def test_degraded_response(self) -> None:
        """Test detecting degraded health."""
        config = throttle.ThrottleConfig(window_size=1)
        controller = throttle.ThrottleController(config)

        controller.record_response(3.0)  # Between healthy and degraded threshold

        assert controller.current_health == throttle.ServerHealth.DEGRADED

    def test_stressed_response(self) -> None:
        """Test detecting stressed health."""
        config = throttle.ThrottleConfig(window_size=1)
        controller = throttle.ThrottleController(config)

        controller.record_response(7.0)  # Between degraded and stressed threshold

        assert controller.current_health == throttle.ServerHealth.STRESSED

    def test_overloaded_response(self) -> None:
        """Test detecting overloaded health."""
        config = throttle.ThrottleConfig(window_size=1)
        controller = throttle.ThrottleController(config)

        controller.record_response(15.0)  # Above stressed threshold

        assert controller.current_health == throttle.ServerHealth.OVERLOADED

    def test_rolling_window(self) -> None:
        """Test rolling window for response times."""
        config = throttle.ThrottleConfig(window_size=3)
        controller = throttle.ThrottleController(config)

        controller.record_response(1.0)
        controller.record_response(1.0)
        controller.record_response(1.0)
        controller.record_response(1.0)

        # Should only keep last 3 values
        assert len(controller.response_times) == 3

    def test_health_recovery(self) -> None:
        """Test health recovery with consecutive fast responses."""
        config = throttle.ThrottleConfig(
            window_size=1,
            recovery_requests=2,
        )
        controller = throttle.ThrottleController(config)

        # First, get into degraded state
        controller.record_response(4.0)
        assert controller.current_health == throttle.ServerHealth.DEGRADED

        # Record fast responses
        controller.record_response(1.0)  # First fast response
        assert controller.current_health == throttle.ServerHealth.DEGRADED

        controller.record_response(1.0)  # Second fast response - should recover
        assert controller.current_health == throttle.ServerHealth.HEALTHY  # type: ignore[comparison-overlap]
        assert controller.stats.health_recoveries == 1  # type: ignore[unreachable]

    def test_get_delay(self) -> None:
        """Test getting delay based on health."""
        config = throttle.ThrottleConfig(
            window_size=1,
            healthy_delay=0.0,
            degraded_delay=1.0,
        )
        controller = throttle.ThrottleController(config)

        assert controller.get_delay() == 0.0

        controller.record_response(4.0)  # Trigger degraded
        assert controller.get_delay() == 1.0

    def test_get_batch_size(self) -> None:
        """Test getting adjusted batch size."""
        config = throttle.ThrottleConfig(
            window_size=1,
            healthy_batch_multiplier=1.0,
            degraded_batch_multiplier=0.5,
        )
        controller = throttle.ThrottleController(config)

        assert controller.get_batch_size(100) == 100

        controller.record_response(4.0)  # Trigger degraded
        assert controller.get_batch_size(100) == 50
        assert controller.stats.batch_size_reductions == 1

    def test_min_batch_size(self) -> None:
        """Test minimum batch size enforcement."""
        config = throttle.ThrottleConfig(
            window_size=1,
            overloaded_batch_multiplier=0.1,
            min_batch_size=5,
        )
        controller = throttle.ThrottleController(config)

        controller.record_response(15.0)  # Trigger overloaded
        # 10 * 0.1 = 1, but min is 5
        assert controller.get_batch_size(10) == 5

    def test_record_error(self) -> None:
        """Test recording server errors."""
        config = throttle.ThrottleConfig(window_size=1)
        controller = throttle.ThrottleController(config)

        controller.record_error(is_server_error=True)

        # Should treat as very slow response
        assert controller.current_health in (
            throttle.ServerHealth.STRESSED,
            throttle.ServerHealth.OVERLOADED,
        )

    def test_get_health_status(self) -> None:
        """Test getting health status dict."""
        controller = throttle.ThrottleController()
        controller.record_response(1.0)

        status = controller.get_health_status()

        assert status["health"] == throttle.ServerHealth.HEALTHY
        assert status["avg_response_time"] == 1.0
        assert status["current_delay"] == 0.0
        assert status["batch_size_factor"] == 1.0

    def test_stats_tracking(self) -> None:
        """Test statistics tracking."""
        controller = throttle.ThrottleController()

        controller.record_response(1.0)
        controller.record_response(2.0)
        controller.record_response(0.5)

        assert controller.stats.total_requests == 3
        assert controller.stats.min_response_time == 0.5
        assert controller.stats.max_response_time == 2.0
        assert controller.stats.avg_response_time == 3.5 / 3


class TestCreateThrottleController:
    """Tests for create_throttle_controller factory."""

    def test_default_controller(self) -> None:
        """Test creating default controller."""
        controller = throttle.create_throttle_controller()

        assert controller.config.healthy_delay == 0.0

    def test_with_base_delay(self) -> None:
        """Test creating controller with base delay."""
        controller = throttle.create_throttle_controller(base_delay=1.0)

        assert controller.config.healthy_delay == 1.0
        assert controller.config.degraded_delay == 1.5

    def test_aggressive_mode(self) -> None:
        """Test creating aggressive controller."""
        controller = throttle.create_throttle_controller(aggressive=True)

        assert controller.config.healthy_threshold == 1.0
        assert controller.config.overloaded_batch_multiplier == 0.1


class TestBatchScaling:
    """Tests for dynamic batch size scaling."""

    def test_healthy_returns_full_batch_size(self) -> None:
        """Test that healthy state returns full batch size."""
        controller = throttle.ThrottleController()
        controller.record_response(1.0)  # Healthy response

        assert controller.get_batch_size(100) == 100

    def test_degraded_reduces_batch_size(self) -> None:
        """Test that degraded state reduces batch size to 75%."""
        config = throttle.ThrottleConfig(window_size=1)
        controller = throttle.ThrottleController(config)

        controller.record_response(4.0)  # Degraded response

        assert controller.get_batch_size(100) == 75

    def test_stressed_reduces_batch_size(self) -> None:
        """Test that stressed state reduces batch size to 50%."""
        config = throttle.ThrottleConfig(window_size=1)
        controller = throttle.ThrottleController(config)

        controller.record_response(7.0)  # Stressed response

        assert controller.get_batch_size(100) == 50

    def test_overloaded_reduces_batch_size(self) -> None:
        """Test that overloaded state reduces batch size to 25%."""
        config = throttle.ThrottleConfig(window_size=1)
        controller = throttle.ThrottleController(config)

        controller.record_response(15.0)  # Overloaded response

        assert controller.get_batch_size(100) == 25

    def test_min_batch_size_enforced(self) -> None:
        """Test that minimum batch size is enforced."""
        config = throttle.ThrottleConfig(
            window_size=1,
            overloaded_batch_multiplier=0.1,
            min_batch_size=10,
        )
        controller = throttle.ThrottleController(config)

        controller.record_response(15.0)  # Overloaded

        # 20 * 0.1 = 2, but min is 10
        assert controller.get_batch_size(20) == 10

    def test_batch_size_recovery(self) -> None:
        """Test that batch size recovers when health improves."""
        config = throttle.ThrottleConfig(
            window_size=1,
            recovery_requests=2,
        )
        controller = throttle.ThrottleController(config)

        # Get into degraded state
        controller.record_response(4.0)
        assert controller.get_batch_size(100) == 75

        # Recover with fast responses
        controller.record_response(1.0)
        controller.record_response(1.0)

        # Should be back to full size
        assert controller.get_batch_size(100) == 100

"""Tests for the retry module."""

from unittest.mock import MagicMock

from odoo_data_flow.lib import retry


class TestErrorCategorization:
    """Tests for error categorization functions."""

    def test_categorize_transient_timeout(self):
        """Test that timeout errors are categorized as transient."""
        category, pattern = retry.categorize_error("Connection timed out")
        assert category == retry.ErrorCategory.TRANSIENT
        assert pattern == "timed out"

    def test_categorize_transient_502(self):
        """Test that 502 errors are categorized as transient."""
        category, pattern = retry.categorize_error("502 Bad Gateway")
        assert category == retry.ErrorCategory.TRANSIENT
        assert pattern == "502"

    def test_categorize_transient_deadlock(self):
        """Test that deadlock errors are categorized as transient."""
        category, pattern = retry.categorize_error(
            "could not serialize access due to concurrent update"
        )
        assert category == retry.ErrorCategory.TRANSIENT
        assert pattern == "could not serialize access"

    def test_categorize_transient_connection_pool(self):
        """Test that connection pool errors are categorized as transient."""
        category, pattern = retry.categorize_error("Connection pool is full")
        assert category == retry.ErrorCategory.TRANSIENT
        assert pattern == "connection pool"

    def test_categorize_permanent_unique_constraint(self):
        """Test that unique constraint errors are categorized as permanent."""
        category, pattern = retry.categorize_error(
            "duplicate key value violates unique constraint"
        )
        assert category == retry.ErrorCategory.PERMANENT
        assert pattern in ("unique constraint", "duplicate key", "violates unique")

    def test_categorize_permanent_access_denied(self):
        """Test that access denied errors are categorized as permanent."""
        category, pattern = retry.categorize_error("Access denied for operation")
        assert category == retry.ErrorCategory.PERMANENT
        assert pattern == "access denied"

    def test_categorize_permanent_field_not_exist(self):
        """Test that field not exist errors are categorized as permanent."""
        category, pattern = retry.categorize_error(
            "Unknown field 'foo' on model 'res.partner'"
        )
        assert category == retry.ErrorCategory.PERMANENT
        assert pattern == "unknown field"

    def test_categorize_recoverable_missing_reference(self):
        """Test that missing reference errors are categorized as recoverable."""
        category, pattern = retry.categorize_error(
            "No matching record found for external id 'base.partner_123'"
        )
        assert category == retry.ErrorCategory.RECOVERABLE
        # Pattern matching is order-dependent
        assert pattern in ("no matching record found", "external id")

    def test_categorize_recoverable_company(self):
        """Test that company errors are categorized as recoverable."""
        category, pattern = retry.categorize_error(
            "Access to unauthorized company records"
        )
        assert category == retry.ErrorCategory.RECOVERABLE
        assert pattern == "company"

    def test_categorize_unknown_is_permanent(self):
        """Test that unknown errors default to permanent."""
        category, pattern = retry.categorize_error("Some weird error happened")
        assert category == retry.ErrorCategory.PERMANENT
        assert pattern == "unknown"


class TestBackoffDelay:
    """Tests for backoff delay calculation."""

    def test_exponential_backoff_increases(self):
        """Test that delay increases exponentially with attempts."""
        config = retry.RetryConfig(base_delay=1.0, exponential_base=2.0, jitter=False)

        delay1 = retry.calculate_backoff_delay(1, config)
        delay2 = retry.calculate_backoff_delay(2, config)
        delay3 = retry.calculate_backoff_delay(3, config)

        assert delay1 == 1.0
        assert delay2 == 2.0
        assert delay3 == 4.0

    def test_max_delay_caps_backoff(self):
        """Test that delay is capped at max_delay."""
        config = retry.RetryConfig(
            base_delay=1.0, exponential_base=2.0, max_delay=5.0, jitter=False
        )

        delay = retry.calculate_backoff_delay(10, config)
        assert delay == 5.0

    def test_jitter_adds_variation(self):
        """Test that jitter adds variation to delay."""
        config = retry.RetryConfig(base_delay=1.0, jitter=True)

        delays = [retry.calculate_backoff_delay(1, config) for _ in range(10)]

        # With jitter, not all delays should be exactly the same
        assert len(set(delays)) > 1


class TestRetryWithBackoff:
    """Tests for retry_with_backoff function."""

    def test_succeeds_first_try(self):
        """Test that successful first attempt returns immediately."""
        func = MagicMock(return_value="success")

        result, error = retry.retry_with_backoff(func)

        assert result == "success"
        assert error is None
        func.assert_called_once()

    def test_succeeds_after_transient_error(self):
        """Test retry succeeds after transient error."""
        call_count = 0

        def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise Exception("Connection timed out")
            return "success"

        config = retry.RetryConfig(max_retries=3, base_delay=0.01)
        result, error = retry.retry_with_backoff(flaky_func, config)

        assert result == "success"
        assert error is None
        assert call_count == 2

    def test_fails_on_permanent_error(self):
        """Test that permanent errors don't retry."""
        func = MagicMock(side_effect=Exception("Duplicate key violates unique"))

        config = retry.RetryConfig(max_retries=3, base_delay=0.01)
        result, error = retry.retry_with_backoff(func, config)

        assert result is None
        assert "Duplicate key" in error
        func.assert_called_once()  # Only one attempt

    def test_max_retries_exceeded(self):
        """Test that retries stop after max_retries."""
        call_count = 0

        def always_fails():
            nonlocal call_count
            call_count += 1
            raise Exception("Connection timed out")

        config = retry.RetryConfig(max_retries=3, base_delay=0.01)
        result, error = retry.retry_with_backoff(always_fails, config)

        assert result is None
        assert error is not None
        assert call_count == 4  # Initial + 3 retries

    def test_stats_are_updated(self):
        """Test that retry stats are updated correctly."""
        call_count = 0

        def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise Exception("502 Bad Gateway")
            return "success"

        config = retry.RetryConfig(max_retries=3, base_delay=0.01)
        stats = retry.RetryStats()

        result, _error = retry.retry_with_backoff(flaky_func, config, stats)

        assert result == "success"
        assert stats.total_attempts == 2
        assert stats.successful_retries == 1
        assert stats.transient_errors == 1

    def test_on_retry_callback(self):
        """Test that on_retry callback is called."""
        call_count = 0

        def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise Exception("Connection timed out")
            return "success"

        callback = MagicMock()
        config = retry.RetryConfig(max_retries=3, base_delay=0.01)

        retry.retry_with_backoff(flaky_func, config, on_retry=callback)

        callback.assert_called_once()
        assert callback.call_args[0][0] == 1  # attempt number


class TestHelperFunctions:
    """Tests for helper functions."""

    def test_should_retry_transient(self):
        """Test should_retry_error for transient errors."""
        assert retry.should_retry_error("Connection timed out") is True
        assert retry.should_retry_error("502 Bad Gateway") is True

    def test_should_not_retry_permanent(self):
        """Test should_retry_error for permanent errors."""
        assert retry.should_retry_error("Duplicate key") is False
        assert retry.should_retry_error("Access denied") is False

    def test_is_recoverable(self):
        """Test is_recoverable_error function."""
        assert retry.is_recoverable_error("No matching record found") is True
        assert retry.is_recoverable_error("Company mismatch") is True
        assert retry.is_recoverable_error("Timeout") is False

    def test_get_retry_recommendation_transient(self):
        """Test recommendation for transient errors."""
        rec = retry.get_retry_recommendation("Connection timed out")

        assert rec["category"] == "transient"
        assert rec["should_retry"] is True
        assert rec["action"] == "retry"

    def test_get_retry_recommendation_permanent(self):
        """Test recommendation for permanent errors."""
        rec = retry.get_retry_recommendation("Duplicate key violation")

        assert rec["category"] == "permanent"
        assert rec["should_retry"] is False
        assert rec["action"] == "fail"

    def test_get_retry_recommendation_recoverable_company(self):
        """Test recommendation for company errors."""
        rec = retry.get_retry_recommendation("Access to unauthorized company")

        assert rec["category"] == "recoverable"
        assert rec["action"] == "adjust_context"
        assert "--all-companies" in rec["message"]

    def test_get_retry_recommendation_recoverable_reference(self):
        """Test recommendation for reference errors."""
        rec = retry.get_retry_recommendation("Reference not found in res.partner")

        assert rec["category"] == "recoverable"
        assert rec["action"] == "skip_or_create"


class TestRetryStats:
    """Tests for RetryStats dataclass."""

    def test_record_error_transient(self):
        """Test recording transient errors."""
        stats = retry.RetryStats()
        stats.record_error(retry.ErrorCategory.TRANSIENT, "timeout")

        assert stats.transient_errors == 1
        assert stats.error_counts["timeout"] == 1

    def test_record_error_permanent(self):
        """Test recording permanent errors."""
        stats = retry.RetryStats()
        stats.record_error(retry.ErrorCategory.PERMANENT, "unique constraint")

        assert stats.permanent_errors == 1
        assert stats.error_counts["unique constraint"] == 1

    def test_record_multiple_errors(self):
        """Test recording multiple errors."""
        stats = retry.RetryStats()
        stats.record_error(retry.ErrorCategory.TRANSIENT, "timeout")
        stats.record_error(retry.ErrorCategory.TRANSIENT, "timeout")
        stats.record_error(retry.ErrorCategory.PERMANENT, "constraint")

        assert stats.transient_errors == 2
        assert stats.permanent_errors == 1
        assert stats.error_counts["timeout"] == 2
        assert stats.error_counts["constraint"] == 1

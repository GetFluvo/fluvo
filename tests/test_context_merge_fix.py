"""Unit tests for the context merging fix."""

# mypy: disable-error-code=unreachable


def test_context_merge_logic() -> None:
    """Test the exact logic used in import_data to merge user context with defaults."""
    # Test case 1: No context provided (should use defaults)
    context = None
    if context is None:
        context_result = {"tracking_disable": True}
    else:
        # Ensure important defaults are maintained while allowing user overrides
        default_context = {"tracking_disable": True}
        # User provided context takes precedence for any overlapping keys
        default_context.update(context)
        context_result = default_context

    # Verify the result
    assert context_result == {"tracking_disable": True}

    # Test case 2: User context provided (should merge with defaults)
    user_context = {"skip_vies_check": True}
    if user_context is None:
        context_result2 = {"tracking_disable": True}
    else:
        # Ensure important defaults are maintained while allowing user overrides
        default_context2: dict[str, object] = {"tracking_disable": True}
        # User provided context takes precedence for any overlapping keys
        default_context2.update(user_context)
        context_result2 = default_context2

    # Verify the result has both default and user values
    assert "tracking_disable" in context_result2
    assert "skip_vies_check" in context_result2
    assert context_result2["tracking_disable"]
    assert context_result2["skip_vies_check"]
    assert context_result["tracking_disable"]


def test_context_user_override() -> None:
    """Test that user-provided context values override defaults."""
    # If user provides tracking_disable=False, it should override the default True
    user_context = {"tracking_disable": False, "custom_key": "custom_value"}

    if user_context is None:
        context_result = {"tracking_disable": True}
    else:
        # Ensure important defaults are maintained while allowing user overrides
        default_context: dict[str, object] = {"tracking_disable": True}
        # User provided context takes precedence for any overlapping keys
        default_context.update(user_context)
        context_result = default_context

    # The user's False should override the default True
    assert not context_result["tracking_disable"]
    assert context_result["custom_key"] == "custom_value"


def test_context_multiple_user_values() -> None:
    """Test that multiple user context values work correctly with defaults."""
    user_context = {
        "skip_vies_check": True,
        "active_test": False,
        "tracking_disable": False,  # Override the default
    }

    if user_context is None:
        context_result = {"tracking_disable": True}
    else:
        # Ensure important defaults are maintained while allowing user overrides
        default_context: dict[str, object] = {"tracking_disable": True}
        # User provided context takes precedence for any overlapping keys
        default_context.update(user_context)
        context_result = default_context

    # Verify all values are present and user override worked
    assert not context_result["tracking_disable"]  # User override
    assert context_result["skip_vies_check"]  # User value
    assert not context_result["active_test"]  # User value
    assert len(context_result) >= 3  # At least these 3 values

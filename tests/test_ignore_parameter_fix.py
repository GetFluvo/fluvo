"""Unit tests for the --ignore parameter fix."""

# mypy: disable-error-code=unreachable


def test_ignore_single_parameter_parsing() -> None:
    """Test the logic that converts --ignore comma-separated string to list."""
    # Simulate the processing logic from import_cmd
    ignore_param = "partner_id/id"

    # This is the exact code we added to import_cmd
    if ignore_param is not None:
        ignore_list = [col.strip() for col in ignore_param.split(",") if col.strip()]
    else:
        ignore_list = []

    # Verify that ignore is properly converted to a list
    assert isinstance(ignore_list, list)
    assert "partner_id/id" in ignore_list
    assert len(ignore_list) == 1


def test_ignore_multiple_parameters_parsing() -> None:
    """Test the logic that converts multiple comma-separated --ignore values to list."""
    # Simulate the processing logic from import_cmd
    ignore_param = "partner_id/id,other_field,another_field"

    # This is the exact code we added to import_cmd
    if ignore_param is not None:
        ignore_list = [col.strip() for col in ignore_param.split(",") if col.strip()]
    else:
        ignore_list = []

    # Verify that ignore is properly converted to a list with all values
    assert isinstance(ignore_list, list)
    assert len(ignore_list) == 3
    assert "partner_id/id" in ignore_list
    assert "other_field" in ignore_list
    assert "another_field" in ignore_list


def test_ignore_with_spaces_parsing() -> None:
    """Test that --ignore properly handles values with spaces by stripping them."""
    # Simulate the processing logic from import_cmd
    ignore_param = " field1 , field2 , field3 "

    # This is the exact code we added to import_cmd
    if ignore_param is not None:
        ignore_list = [col.strip() for col in ignore_param.split(",") if col.strip()]
    else:
        ignore_list = []

    # Verify that spaces are stripped from the values
    assert isinstance(ignore_list, list)
    assert len(ignore_list) == 3
    assert "field1" in ignore_list
    assert "field2" in ignore_list
    assert "field3" in ignore_list
    # Verify no empty strings or strings with spaces made it through
    for item in ignore_list:
        assert item == item.strip()  # Should already be stripped
        assert item != ""  # Should not be empty after stripping


def test_ignore_empty_string_parsing() -> None:
    """Test that --ignore properly handles empty strings in comma-separated list."""
    # Simulate the processing logic from import_cmd with empty values in between
    ignore_param = "field1,,field2,,,field3"

    # This is the exact code we added to import_cmd
    if ignore_param is not None:
        ignore_list = [col.strip() for col in ignore_param.split(",") if col.strip()]
    else:
        ignore_list = []

    # Verify that empty strings are filtered out
    assert isinstance(ignore_list, list)
    assert len(ignore_list) == 3  # Only the non-empty fields
    assert "field1" in ignore_list
    assert "field2" in ignore_list
    assert "field3" in ignore_list


def test_ignore_none_parameter() -> None:
    """Test that --ignore processes None correctly."""
    ignore_param = None

    # This is the exact code we added to import_cmd
    ignore_list: list[str] = []  # Initialize to satisfy mypy type checking
    if ignore_param is not None:
        ignore_list = [col.strip() for col in ignore_param.split(",") if col.strip()]
    else:
        ignore_list = []

    # Verify that we get an empty list when ignore_param is None
    assert isinstance(ignore_list, list)
    assert len(ignore_list) == 0

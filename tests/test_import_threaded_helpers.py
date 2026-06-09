"""Unit tests for import_threaded helper functions (coverage)."""

from unittest.mock import MagicMock

import pytest

from fluvo.import_threaded import (
    _convert_external_id_field,
    _extract_access_error_message,
    _handle_create_error,
    _prepare_pass_2_data,
    _process_external_id_fields,
    _resolve_external_id_for_pass2,
)


# --- _resolve_external_id_for_pass2 ---
def test_resolve_external_id_found() -> None:
    """A matching ir.model.data row resolves to its res_id."""
    proxy = MagicMock()
    proxy.search_read.return_value = [{"res_id": 42}]
    assert _resolve_external_id_for_pass2(proxy, "mymod.rec_a") == 42


def test_resolve_external_id_not_found() -> None:
    """No match across any variation returns None."""
    proxy = MagicMock()
    proxy.search_read.return_value = []
    assert _resolve_external_id_for_pass2(proxy, "mymod.rec_a") is None


# --- _convert_external_id_field ---
def test_convert_external_id_found() -> None:
    """A resolvable external id is converted to its database res_id."""
    conn = MagicMock()
    imd = conn.get_model.return_value
    imd.search.return_value = [5]
    imd.read.return_value = {"res_id": 99}
    base, value = _convert_external_id_field(conn, "parent_id/id", "mod.parent_a")
    assert base == "parent_id"
    assert value == 99


def test_convert_external_id_no_dot_uses_export_module() -> None:
    """A value without a dot is looked up under the __export__ module."""
    conn = MagicMock()
    imd = conn.get_model.return_value
    imd.search.return_value = [5]
    imd.read.return_value = {"res_id": 7}
    base, value = _convert_external_id_field(conn, "x_id/id", "just_name")
    assert value == 7


def test_convert_external_id_not_found_stays_false() -> None:
    """An unresolvable external id leaves the value False."""
    conn = MagicMock()
    conn.get_model.return_value.search.return_value = []
    base, value = _convert_external_id_field(conn, "parent_id/id", "mod.x")
    assert base == "parent_id"
    assert value is False


def test_convert_external_id_handles_exception() -> None:
    """A lookup error is swallowed and the value stays False."""
    conn = MagicMock()
    conn.get_model.return_value.search.side_effect = Exception("boom")
    base, value = _convert_external_id_field(conn, "parent_id/id", "mod.x")
    assert base == "parent_id"
    assert value is False


# --- _process_external_id_fields ---
def test_process_external_id_fields_mixed() -> None:
    """/id fields are converted; plain fields pass through unchanged."""
    conn = MagicMock()
    imd = conn.get_model.return_value
    imd.search.return_value = [5]
    imd.read.return_value = {"res_id": 99}
    converted, ext = _process_external_id_fields(
        conn, {"name": "Acme", "parent_id/id": "mod.parent_a"}
    )
    assert converted["name"] == "Acme"
    assert converted["parent_id"] == 99
    assert ext == ["parent_id/id"]


# --- _extract_access_error_message ---
def test_extract_access_error_accesserror_pattern() -> None:
    """An AccessError(...) wrapper is unwrapped to its message."""
    assert (
        _extract_access_error_message("AccessError('You cannot do this')")
        == "You cannot do this"
    )


def test_extract_access_error_data_message() -> None:
    """A dict error prefers data.message."""
    assert (
        _extract_access_error_message("{'data': {'message': 'data err'}}") == "data err"
    )


def test_extract_access_error_top_message() -> None:
    """A dict error falls back to top-level message."""
    assert _extract_access_error_message("{'message': 'top err'}") == "top err"


def test_extract_access_error_regex_message() -> None:
    """A non-dict string with a 'message': '...' pattern is extracted."""
    assert (
        _extract_access_error_message("blah 'message': 'regex err' blah") == "regex err"
    )


def test_extract_access_error_strips_traceback() -> None:
    """A traceback tail is stripped from the message."""
    out = _extract_access_error_message(
        "Bad thing\nTraceback (most recent call last):\n  ..."
    )
    assert "Traceback" not in out
    assert "Bad thing" in out


def test_extract_access_error_truncates_long() -> None:
    """A very long error is truncated with an ellipsis."""
    out = _extract_access_error_message("x" * 250)
    assert out.endswith("...")
    assert len(out) <= 203


@pytest.mark.parametrize(
    "err_text",
    [
        "AccessError: not allowed",
        "check constraint violation",
        "connection pool is full",
        "could not serialize access",
        "tuple index out of range",
        "duplicate key value already exists",
        "some random unmatched error",
    ],
)
def test_handle_create_error_branches(err_text: str) -> None:
    """Each error category produces a message and appends it to the failed line."""
    msg, failed_line, summary = _handle_create_error(
        0, Exception(err_text), ["a", "b"], "Fell back to create"
    )
    assert isinstance(msg, str) and msg
    assert failed_line[-1] == msg  # error appended to the row
    assert failed_line[:-1] == ["a", "b"]
    assert isinstance(summary, str)


def test_handle_create_error_invalid_external_id_field() -> None:
    """An 'invalid field .../id' error is flagged as an external-id problem."""
    msg, _, _ = _handle_create_error(
        2, Exception("Invalid field 'parent_id/id' in model"), ["x"], ""
    )
    assert "Invalid external ID" in msg


def test_prepare_pass_2_data_self_ref_and_m2m() -> None:
    """Resolves a self-ref many2one + a many2many deferred field via id_map."""
    header = ["id", "parent_id/id", "tag_ids/id"]
    all_data = [["child_a", "parent_x", "tag1,tag2"]]
    id_map = {"child_a": 100, "parent_x": 50, "tag1": 11, "tag2": 12}
    deferred = ["parent_id/id", "tag_ids/id"]
    model = MagicMock()
    model.fields_get.return_value = {
        "parent_id": {"type": "many2one"},
        "tag_ids": {"type": "many2many"},
    }
    result = _prepare_pass_2_data(all_data, header, 0, id_map, deferred, model)
    assert len(result) == 1
    db_id, vals = result[0]
    assert db_id == 100
    assert vals["parent_id"] == 50
    assert vals["tag_ids"] == [(6, 0, [11, 12])]


def test_prepare_pass_2_data_no_deferred_in_header() -> None:
    """No deferred column present -> empty result (early return)."""
    result = _prepare_pass_2_data(
        [["a", "A"]], ["id", "name"], 0, {"a": 1}, ["parent_id/id"], MagicMock()
    )
    assert result == []


def test_prepare_pass_2_data_skips_unmapped_rows() -> None:
    """A row whose id is not in id_map is skipped."""
    model = MagicMock()
    model.fields_get.return_value = {"parent_id": {"type": "many2one"}}
    result = _prepare_pass_2_data(
        [["ghost", "p"]], ["id", "parent_id/id"], 0, {}, ["parent_id/id"], model
    )
    assert result == []


def test_resolve_external_id_via_fallback_full_match() -> None:
    """When structured variations miss, the full-string name match resolves it."""
    proxy = MagicMock()
    # 5 structured variations return nothing; the fallback full-match returns a row.
    proxy.search_read.side_effect = [[], [], [], [], [], [{"res_id": 7}]]
    assert _resolve_external_id_for_pass2(proxy, "mymod.rec_a") == 7


def test_prepare_pass_2_data_resolves_via_proxy() -> None:
    """A deferred value not in id_map is resolved through ir.model.data (RPC path)."""
    header = ["id", "user_id/id"]
    all_data = [["rec_a", "base.user_admin"]]
    id_map = {"rec_a": 100}  # the user_id target is NOT in id_map
    deferred = ["user_id/id"]
    model = MagicMock()
    model.fields_get.return_value = {"user_id": {"type": "many2one"}}
    model.connection.model.return_value.search_read.return_value = [{"res_id": 2}]
    result = _prepare_pass_2_data(all_data, header, 0, id_map, deferred, model)
    assert len(result) == 1
    db_id, vals = result[0]
    assert db_id == 100
    assert vals.get("user_id") == 2

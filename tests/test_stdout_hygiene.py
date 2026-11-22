"""stdout must stay clean for a line-protocol consumer of the core (#363).

Logs and progress are never data: a programmatic/server consumer may speak a
line protocol over stdout, so the library must route logging to stderr and the
pure compute layers must print nothing to stdout.
"""

import io
from contextlib import redirect_stdout
from pathlib import Path

from fluvo.lib.assess import assess_model
from fluvo.logging_config import log, setup_logging


def test_logging_does_not_write_to_stdout() -> None:
    """After setup_logging, log records go to stderr, never stdout (#363)."""
    setup_logging(verbose=True)
    buf = io.StringIO()
    with redirect_stdout(buf):
        log.info("an info line")
        log.warning("a warning line")
        log.error("an error line")
    assert buf.getvalue() == ""


def test_pure_assess_layer_emits_nothing_on_stdout() -> None:
    """The pure assess compute layer writes nothing to stdout (#363)."""
    fields = {
        "id": {"type": "integer"},
        "name": {"type": "char", "required": True},
        "company_id": {"type": "many2one"},
    }
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = assess_model("res.partner", fields, row_count=42)
    assert buf.getvalue() == ""
    assert result["model"] == "res.partner"


def test_read_source_file_emits_nothing_on_stdout(tmp_path: Path) -> None:
    """Reading a source file writes nothing to stdout (#363)."""
    from fluvo.lib.compare import read_source_file

    csv = tmp_path / "rows.csv"
    csv.write_text("ref;name\nA1;Acme\nB2;Globex\n")
    buf = io.StringIO()
    with redirect_stdout(buf):
        frame = read_source_file(str(csv), delimiter=";")
    assert buf.getvalue() == ""
    assert frame.height == 2

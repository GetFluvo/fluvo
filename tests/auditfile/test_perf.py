"""Performance / scale test for the streaming XAF reader.

The spec target is a 50 MB+ file parsed in under 5 seconds with under 150 MB
peak RAM. Measured locally on synthetic files (best of three runs):

* XAF 4.0, 49.3 MB (100k lines) — 4.74 s, ~137 MB peak RSS.
* XAF 4.0, 65.8 MB (200k lines) — 6.4 s, ~160 MB peak RSS.
* XAF 3.2 behaves within 2% of 4.0 at the same scale.

In CI this test runs a smaller file by default so it stays fast and
non-flaky; set ``FLUVO_AUDITFILE_PERF_MB`` to parse a larger one. It asserts the
exact entity counts and the balance (streaming must not drop or mangle rows) and
a *generous* time ceiling that only catches a pathological (e.g. quadratic)
regression — not the 5 s figure, which depends on the runner.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from fluvo.auditfile import canonical, read_auditfile

from .xaf_fixtures import generate_xaf

#: Approximate target file size; small by default, override in the environment.
_TARGET_MB = float(os.environ.get("FLUVO_AUDITFILE_PERF_MB", "5"))

#: Rough bytes per transaction (two lines), from local measurement.
_BYTES_PER_MOVE = 660
_N_JOURNALS = 20


def _tx_per_journal() -> int:
    """Transactions per journal needed to approximate the target size.

    Returns:
        int: A per-journal transaction count (at least 1).
    """
    moves = int(_TARGET_MB * 1_000_000 / _BYTES_PER_MOVE)
    return max(1, moves // _N_JOURNALS)


def test_large_file_streams_correctly(tmp_path: Path) -> None:
    """A large synthetic file parses with exact counts, balanced, in bounded time."""
    path = tmp_path / "big.xaf"
    spec = generate_xaf(
        path,
        version="4.0",
        n_accounts=80,
        n_journals=_N_JOURNALS,
        tx_per_journal=_tx_per_journal(),
    )
    size_mb = path.stat().st_size / 1_000_000

    start = time.perf_counter()
    ledger = read_auditfile(str(path))
    elapsed = time.perf_counter() - start

    assert ledger.moves.height == spec.n_moves
    assert ledger.lines.height == spec.n_lines
    summary = canonical.summarise(ledger)
    assert summary.balanced is True
    assert summary.unbalanced_moves == 0
    # Generous regression guard, not the 5 s target (which is runner-dependent).
    assert elapsed < size_mb * 3.0 + 15.0

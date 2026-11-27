"""Classify a failed-import row by *who decides* what to do about it (#299).

A flat fail file mixes several distinct populations, each needing a different
decision by a different person. Sorting failures into named **buckets** — keyed
on the owner of the decision, not on what is technically wrong — is what makes a
fail file usable as a client-facing artifact.

This module is the pure classifier: a single ``(error_reason) -> bucket``
function driven by a small, conservative rule table. It makes no network calls
and reads no files beyond an optional post-hoc tally over a finished fail file.
Most reasons fall through to :data:`UNCLASSIFIED` at first, which is honest —
the taxonomy grows as real reasons are seen, never by guessing.

The four buckets, by owner:

- :data:`FLUVO_FIXABLE` — Fluvo can propose and apply a correction (source data
  damaged before Fluvo saw it: scientific notation, trailing-zero loss).
- :data:`CLIENT_DECISION` — nothing is technically wrong; a person must choose
  (a posted entry that cannot be re-imported, a record missing a business
  prerequisite).
- :data:`BLOCKED_MISSING_DATA` — blocked on reference data that does not exist
  yet (an unresolved external id, a missing category or parent).
- :data:`UNCLASSIFIED` — not yet recognised; the honest default.
"""

import csv
import re
from collections import Counter

FLUVO_FIXABLE = "fluvo_fixable"
CLIENT_DECISION = "client_decision"
BLOCKED_MISSING_DATA = "blocked_missing_data"
UNCLASSIFIED = "unclassified"

#: The internal fail-file column holding a row's bucket, written next to
#: ``_ERROR_REASON``. Stripped on ``--fail`` re-import like ``_ERROR_REASON``.
BUCKET_COLUMN = "_FLUVO_BUCKET"

# Ordered, conservative rules: the FIRST pattern whose regex is found (case
# -insensitively) in the error reason wins. Only high-confidence patterns appear
# here; anything unmatched is UNCLASSIFIED on purpose. Order matters — put the
# more specific reason first when two could match.
_RULES: tuple[tuple[str, str], ...] = (
    # Blocked on reference data that does not exist in the target yet.
    (BLOCKED_MISSING_DATA, r"no matching record found"),
    (BLOCKED_MISSING_DATA, r"\bunknown\b.*\bexternal id\b"),
    (BLOCKED_MISSING_DATA, r"\bno (?:value|record)s? found\b"),
    (BLOCKED_MISSING_DATA, r"missing required"),
    (BLOCKED_MISSING_DATA, r"does not exist"),
    # A person must choose: a posted move cannot be re-imported (see #358).
    (CLIENT_DECISION, r"posted"),
    (CLIENT_DECISION, r"cannot be modified|is immutable"),
    # A move whose name's year disagrees with its date: Odoo rejects it (at
    # create on 16, at post on 17-19). The canonical reason tag and Odoo's own
    # wording both land here (#358/#359).
    (
        CLIENT_DECISION,
        r"sequence_date_mismatch|match the sequence number|"
        r"aligned with the existing sequence",
    ),
    # Source value damaged before Fluvo saw it; Fluvo can propose a fix.
    (FLUVO_FIXABLE, r"scientific notation|\d[eE][+-]?\d"),
    (FLUVO_FIXABLE, r"invalid literal for int|could not convert"),
)

_COMPILED: tuple[tuple[str, "re.Pattern[str]"], ...] = tuple(
    (bucket, re.compile(pattern, re.IGNORECASE)) for bucket, pattern in _RULES
)


def classify_failure(error_reason: str | None) -> str:
    """Classify one failed row's error reason into an owner bucket.

    Args:
        error_reason: The ``_ERROR_REASON`` text recorded for the failed row.

    Returns:
        str: One of :data:`FLUVO_FIXABLE`, :data:`CLIENT_DECISION`,
        :data:`BLOCKED_MISSING_DATA`, or :data:`UNCLASSIFIED` (the default when
        no rule matches, including an empty or ``None`` reason).
    """
    if not error_reason:
        return UNCLASSIFIED
    for bucket, pattern in _COMPILED:
        if pattern.search(error_reason):
            return bucket
    return UNCLASSIFIED


def bucket_counts_from_fail_file(
    fail_file: str, separator: str = ";", encoding: str = "utf-8"
) -> dict[str, int]:
    """Tally failed rows by bucket from a finished fail file.

    Reads the ``_FLUVO_BUCKET`` column when present, else classifies each row's
    ``_ERROR_REASON``. This is a decoupled, post-hoc read — it never touches the
    import's reconciliation counters or its write path.

    Args:
        fail_file: Path to the fail file to tally.
        separator: The field separator the fail file was written with.
        encoding: The fail file's text encoding.

    Returns:
        dict[str, int]: Bucket name -> row count, for buckets that occur. Empty
        when the file is missing, unreadable, or has no data rows.
    """
    counts: Counter[str] = Counter()
    try:
        with open(fail_file, encoding=encoding, newline="") as handle:
            reader = csv.reader(handle, delimiter=separator)
            header = next(reader, None)
            if not header:
                return {}
            bucket_idx = header.index(BUCKET_COLUMN) if BUCKET_COLUMN in header else -1
            reason_idx = (
                header.index("_ERROR_REASON") if "_ERROR_REASON" in header else -1
            )
            for row in reader:
                if bucket_idx != -1 and bucket_idx < len(row) and row[bucket_idx]:
                    counts[row[bucket_idx]] += 1
                elif reason_idx != -1 and reason_idx < len(row):
                    counts[classify_failure(row[reason_idx])] += 1
                else:
                    counts[UNCLASSIFIED] += 1
    except (OSError, csv.Error):
        return {}
    return dict(counts)

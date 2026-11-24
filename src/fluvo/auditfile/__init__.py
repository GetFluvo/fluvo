"""Audit-file source connectors (XAF first; FEC, SAF-T, DATEV to follow).

A tax-authority audit file is a complete, standardised dump of a company's
financial year — chart of accounts, customers and suppliers, journals and every
transaction. It is the highest-fidelity source for migrating a company's history
into Odoo, and many jurisdictions mandate it, so it is reliably available.

This subpackage treats such a file as something to **compile**, not load
directly. The pipeline is::

    audit file ─reader─▶ CanonicalLedger ─transform─▶ stage dir ─▶ fluvo --flow-file
      (per format)      (format-neutral)   (Odoo-shaped)   CSVs + flows.yml

No new write path to Odoo is introduced: the upsert-on-external-id, fail files
and the ``created + failed + unaccounted == total`` reconciliation contract all
come from the existing flow runner that loads the staged CSVs.

Public entry points:

* :func:`read_auditfile` — a file path to a :class:`CanonicalLedger`.
* :mod:`fluvo.auditfile.canonical` — the canonical model and its summary.
* :mod:`fluvo.auditfile.formats` — the reader registry.
"""

from __future__ import annotations

from ..lib.core import InputError
from .canonical import CanonicalLedger
from .formats import by_name, detect


def read_auditfile(path: str, *, fmt: str | None = None) -> CanonicalLedger:
    """Read an audit file into a canonical ledger.

    Picks a reader by sniffing the file, or uses the named format when ``fmt``
    is given, then returns the validated canonical ledger.

    Args:
        path: The audit file to read.
        fmt: Force a specific format (a reader's ``format_name``, e.g. ``"xaf"``)
            instead of auto-detecting.

    Returns:
        CanonicalLedger: The file as validated, format-neutral frames.

    Raises:
        InputError: If ``fmt`` names no known reader, or no reader recognises
            the file when auto-detecting.
    """
    if fmt is not None:
        reader = by_name(fmt)
        if reader is None:
            raise InputError(f"unknown audit-file format '{fmt}'.")
    else:
        reader = detect(path)
        if reader is None:
            raise InputError(
                f"could not recognise '{path}' as any known audit-file format."
            )
    ledger = reader.read(path)
    ledger.validate()
    return ledger

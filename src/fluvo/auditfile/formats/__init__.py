"""The audit-file format registry.

A *reader* turns one audit-file format into a :class:`CanonicalLedger`. Readers
are looked up here by two questions: *can you read this file?*
(:meth:`FormatReader.detect`) and *read it* (:meth:`FormatReader.read`). The
canonical model downstream never learns which format it came from.

Built-in readers (currently XAF) register at import time. A format may also ship
**out of tree**: any installed distribution that advertises the entry-point
group ``fluvo.auditfile_formats`` contributes a reader, loaded on first use, so a
proprietary or country-specific format need not live in this repository.

The public surface is small: :func:`readers` lists what is available,
:func:`detect` picks the reader for a file, and :func:`read` returns the ledger.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Protocol, runtime_checkable

from ...logging_config import log
from ..canonical import CanonicalLedger
from ..formats import xaf

#: Entry-point group an external distribution advertises to add a reader. Each
#: entry point must load to a zero-argument callable returning a FormatReader.
ENTRY_POINT_GROUP = "fluvo.auditfile_formats"


@runtime_checkable
class FormatReader(Protocol):
    """How the registry drives one audit-file format.

    A reader is stateless: ``detect`` and ``read`` are both pure functions of a
    file path, so the same instance serves every call.
    """

    #: Short, stable format tag (e.g. ``"xaf"``), also the ``--format`` value.
    format_name: str

    def detect(self, path: str) -> bool:
        """Report whether this reader recognises the file at ``path``.

        Must be cheap and must not raise on an unrelated file: it is called
        against every candidate reader. Sniff a few bytes, do not parse the
        whole document.

        Args:
            path: The audit file to probe.

        Returns:
            bool: True if this reader can read the file.
        """
        ...

    def read(self, path: str) -> CanonicalLedger:
        """Read the file at ``path`` into a canonical ledger.

        Args:
            path: The audit file to read.

        Returns:
            CanonicalLedger: The file as format-neutral frames.
        """
        ...


# Built-in readers, registered at import time. External readers are appended
# lazily by _ensure_loaded on first lookup.
_READERS: list[FormatReader] = [xaf.READER]
_external_loaded = False


def register(reader: FormatReader) -> None:
    """Add a reader to the registry.

    Mostly for tests and in-process extensions; shipped formats register
    through the built-in list or the entry-point group instead.

    Args:
        reader: The reader to make available to :func:`detect` and :func:`read`.
    """
    _READERS.append(reader)


def _ensure_loaded() -> None:
    """Load external readers from the entry-point group once, idempotently.

    A badly behaved third-party entry point should not take down a core
    command, so a load failure is swallowed here; the built-in readers remain
    available regardless.
    """
    global _external_loaded
    if _external_loaded:
        return
    _external_loaded = True
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        try:
            factory = entry.load()
            _READERS.append(factory())
        except Exception as exc:
            log.warning("ignoring audit-file format plugin %r: %s", entry.name, exc)
            continue


def readers() -> list[FormatReader]:
    """List every available reader, built-in and external.

    Returns:
        list[FormatReader]: The registered readers; external ones are loaded on
        the first call.
    """
    _ensure_loaded()
    return list(_READERS)


def by_name(name: str) -> FormatReader | None:
    """Find a reader by its :attr:`FormatReader.format_name`.

    Args:
        name: A format tag, e.g. ``"xaf"``.

    Returns:
        FormatReader | None: The matching reader, or None if none matches.
    """
    for reader in readers():
        if reader.format_name == name:
            return reader
    return None


def detect(path: str) -> FormatReader | None:
    """Pick the reader that recognises the file at ``path``.

    Args:
        path: The audit file to probe.

    Returns:
        FormatReader | None: The first reader whose ``detect`` returns True, or
        None when no registered reader recognises the file.
    """
    for reader in readers():
        if reader.detect(path):
            return reader
    return None

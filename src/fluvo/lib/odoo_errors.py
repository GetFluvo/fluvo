"""Extract the human-readable message out of an Odoo RPC error.

An Odoo RPC fault stringifies to a whole nested dict whose text is a server-side
traceback — useless in a fail file or a console line. The one useful part is the
fault's message. This module holds the single extractor the load path and the
post step both use, so both surface the same clean message rather than the raw
dict.
"""

from __future__ import annotations

import ast
from typing import Any


def _message_from_fault(fault: dict[str, Any]) -> str:
    """Pull the message out of a parsed Odoo fault dict.

    Args:
        fault: A fault mapping (the RPC error's dict form).

    Returns:
        str: ``data.message``, else ``data.arguments[0]``, else the top-level
        ``message``; ``""`` when none is a non-empty string.
    """
    data = fault.get("data")
    if isinstance(data, dict):
        message = data.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        arguments = data.get("arguments")
        if (
            isinstance(arguments, (list, tuple))
            and arguments
            and isinstance(arguments[0], str)
            and arguments[0].strip()
        ):
            return arguments[0].strip()
    message = fault.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return ""


def format_odoo_error(error: Any) -> str:
    """Return a concise, human-readable message for an Odoo RPC error.

    Works on the raised exception (whose first arg is often the fault dict) or
    on its already-stringified form. Prefers ``data.message``, then
    ``data.arguments[0]``, then the top-level ``message``; otherwise the error's
    own text with hard newlines flattened to spaces.

    Args:
        error: The exception, fault dict, or error string.

    Returns:
        str: The cleaned message.
    """
    if (
        isinstance(error, BaseException)
        and error.args
        and isinstance(error.args[0], dict)
    ):
        message = _message_from_fault(error.args[0])
        if message:
            return message
    text = str(error)
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        parsed = None
    if isinstance(parsed, dict):
        message = _message_from_fault(parsed)
        if message:
            return message
    return text.strip().replace("\n", " ")

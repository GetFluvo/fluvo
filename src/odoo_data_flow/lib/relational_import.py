"""Handles relational import strategies like m2m and o2m."""

from .relational_import_strategies.direct import (
    run_direct_relational_import,
)
from .relational_import_strategies.write_o2m_tuple import (
    run_write_o2m_tuple_import,
)
from .relational_import_strategies.write_tuple import (
    run_write_tuple_import,
)

# Re-export the main functions
__all__ = [
    "run_direct_relational_import",
    "run_write_o2m_tuple_import",
    "run_write_tuple_import",
]

"""Checkpoint management for resumable imports.

This module provides functionality to save and restore import progress,
allowing imports to resume from where they left off after a crash or
interruption.
"""

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..logging_config import log

# Default checkpoint directory name
CHECKPOINT_DIR = ".odf_checkpoint"


@dataclass
class CheckpointData:
    """Data structure for import checkpoint state."""

    session_id: str
    file_path: str
    file_hash: str
    model: str
    config_hash: str
    last_completed_batch: int
    total_batches: int
    records_processed: int
    records_created: int
    records_failed: int
    id_map: dict[str, int] = field(default_factory=dict)
    deferred_fields: list[str] = field(default_factory=list)
    pass_1_complete: bool = False
    pass_2_complete: bool = False
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


def _compute_file_hash(file_path: str) -> str:
    """Compute a hash of the file contents for change detection.

    Uses first 1MB + last 1MB + file size for efficiency on large files.
    """
    try:
        file_size = os.path.getsize(file_path)
        hasher = hashlib.sha256()
        hasher.update(str(file_size).encode())

        with open(file_path, "rb") as f:
            # Read first 1MB
            hasher.update(f.read(1024 * 1024))

            # Read last 1MB if file is large enough
            if file_size > 2 * 1024 * 1024:
                f.seek(-1024 * 1024, 2)
                hasher.update(f.read())

        return hasher.hexdigest()[:16]
    except Exception as e:
        log.warning(f"Could not compute file hash: {e}")
        return "unknown"


def _compute_config_hash(config: Any) -> str:
    """Compute a hash of the configuration for session identification."""
    if isinstance(config, str):
        config_str = config
    elif isinstance(config, dict):
        config_str = json.dumps(config, sort_keys=True)
    else:
        config_str = str(config)

    return hashlib.sha256(config_str.encode()).hexdigest()[:16]


def generate_session_id(file_path: str, config: Any, model: str) -> str:
    """Generate a unique session ID for this import operation.

    The session ID is based on:
    - Absolute file path
    - Configuration (connection details)
    - Model name
    """
    abs_path = os.path.abspath(file_path)
    config_hash = _compute_config_hash(config)
    combined = f"{abs_path}:{config_hash}:{model}"
    return hashlib.sha256(combined.encode()).hexdigest()[:32]


def get_checkpoint_dir(file_path: str) -> Path:
    """Get the checkpoint directory for a given data file."""
    return Path(file_path).parent / CHECKPOINT_DIR


def get_checkpoint_path(file_path: str, session_id: str) -> Path:
    """Get the checkpoint file path for a given session."""
    checkpoint_dir = get_checkpoint_dir(file_path)
    return checkpoint_dir / f"{session_id}.json"


def save_checkpoint(checkpoint: CheckpointData) -> bool:
    """Save checkpoint data to disk.

    Args:
        checkpoint: The checkpoint data to save.

    Returns:
        True if save was successful, False otherwise.
    """
    try:
        checkpoint_dir = get_checkpoint_dir(checkpoint.file_path)
        checkpoint_dir.mkdir(parents=True, exist_ok=True)

        checkpoint_path = get_checkpoint_path(
            checkpoint.file_path, checkpoint.session_id
        )

        # Update timestamp
        checkpoint.timestamp = datetime.now().isoformat()

        # Convert to dict for JSON serialization
        data = {
            "session_id": checkpoint.session_id,
            "file_path": checkpoint.file_path,
            "file_hash": checkpoint.file_hash,
            "model": checkpoint.model,
            "config_hash": checkpoint.config_hash,
            "last_completed_batch": checkpoint.last_completed_batch,
            "total_batches": checkpoint.total_batches,
            "records_processed": checkpoint.records_processed,
            "records_created": checkpoint.records_created,
            "records_failed": checkpoint.records_failed,
            "id_map": checkpoint.id_map,
            "deferred_fields": checkpoint.deferred_fields,
            "pass_1_complete": checkpoint.pass_1_complete,
            "pass_2_complete": checkpoint.pass_2_complete,
            "timestamp": checkpoint.timestamp,
        }

        # Write atomically using temp file
        temp_path = checkpoint_path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        temp_path.replace(checkpoint_path)

        log.debug(
            f"Checkpoint saved: batch {checkpoint.last_completed_batch}, "
            f"{checkpoint.records_processed} records processed"
        )
        return True

    except Exception as e:
        log.warning(f"Failed to save checkpoint: {e}")
        return False


def load_checkpoint(
    file_path: str, config: Any, model: str
) -> Optional[CheckpointData]:
    """Load checkpoint data from disk if available and valid.

    Args:
        file_path: Path to the data file being imported.
        config: Connection configuration.
        model: Odoo model name.

    Returns:
        CheckpointData if a valid checkpoint exists, None otherwise.
    """
    try:
        session_id = generate_session_id(file_path, config, model)
        checkpoint_path = get_checkpoint_path(file_path, session_id)

        if not checkpoint_path.exists():
            return None

        with open(checkpoint_path, encoding="utf-8") as f:
            data = json.load(f)

        # Verify file hasn't changed
        current_hash = _compute_file_hash(file_path)
        if data.get("file_hash") != current_hash:
            log.warning(
                "Data file has changed since last checkpoint. "
                "Cannot resume - starting fresh."
            )
            delete_checkpoint(file_path, session_id)
            return None

        checkpoint = CheckpointData(
            session_id=data["session_id"],
            file_path=data["file_path"],
            file_hash=data["file_hash"],
            model=data["model"],
            config_hash=data["config_hash"],
            last_completed_batch=data["last_completed_batch"],
            total_batches=data["total_batches"],
            records_processed=data["records_processed"],
            records_created=data["records_created"],
            records_failed=data["records_failed"],
            id_map=data.get("id_map", {}),
            deferred_fields=data.get("deferred_fields", []),
            pass_1_complete=data.get("pass_1_complete", False),
            pass_2_complete=data.get("pass_2_complete", False),
            timestamp=data["timestamp"],
        )

        log.info(
            f"Found checkpoint from {checkpoint.timestamp}: "
            f"batch {checkpoint.last_completed_batch}/{checkpoint.total_batches}, "
            f"{checkpoint.records_processed} records processed"
        )

        return checkpoint

    except json.JSONDecodeError as e:
        log.warning(f"Corrupted checkpoint file: {e}")
        return None
    except Exception as e:
        log.warning(f"Failed to load checkpoint: {e}")
        return None


def delete_checkpoint(file_path: str, session_id: str) -> bool:
    """Delete a checkpoint file.

    Args:
        file_path: Path to the data file.
        session_id: Session ID of the checkpoint to delete.

    Returns:
        True if deletion was successful or file didn't exist, False on error.
    """
    try:
        checkpoint_path = get_checkpoint_path(file_path, session_id)
        if checkpoint_path.exists():
            checkpoint_path.unlink()
            log.debug(f"Deleted checkpoint: {checkpoint_path}")
        return True
    except Exception as e:
        log.warning(f"Failed to delete checkpoint: {e}")
        return False


def cleanup_old_checkpoints(file_path: str, max_age_days: int = 7) -> int:
    """Clean up old checkpoint files.

    Args:
        file_path: Path to the data file (used to find checkpoint dir).
        max_age_days: Maximum age of checkpoints to keep.

    Returns:
        Number of checkpoints deleted.
    """
    try:
        checkpoint_dir = get_checkpoint_dir(file_path)
        if not checkpoint_dir.exists():
            return 0

        deleted = 0
        now = datetime.now()

        for checkpoint_file in checkpoint_dir.glob("*.json"):
            try:
                with open(checkpoint_file, encoding="utf-8") as f:
                    data = json.load(f)

                timestamp = datetime.fromisoformat(data.get("timestamp", ""))
                age_days = (now - timestamp).days

                if age_days > max_age_days:
                    checkpoint_file.unlink()
                    deleted += 1
                    log.debug(f"Cleaned up old checkpoint: {checkpoint_file.name}")

            except Exception:
                # If we can't read it, it's probably corrupted - delete it
                checkpoint_file.unlink()
                deleted += 1

        return deleted

    except Exception as e:
        log.warning(f"Error during checkpoint cleanup: {e}")
        return 0

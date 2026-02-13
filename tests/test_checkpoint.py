"""Tests for the checkpoint module."""

import json
import os
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from odoo_data_flow.lib import checkpoint as ckpt


@pytest.fixture
def temp_dir() -> Generator[str, None, None]:
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def sample_csv(temp_dir: str) -> str:
    """Create a sample CSV file for testing."""
    csv_path = Path(temp_dir) / "test_data.csv"
    csv_path.write_text("id;name\n1;test1\n2;test2\n")
    return str(csv_path)


class TestCheckpointDataStructure:
    """Tests for CheckpointData dataclass."""

    def test_checkpoint_data_defaults(self) -> None:
        """Test that CheckpointData has sensible defaults."""
        cp = ckpt.CheckpointData(
            session_id="test123",
            file_path="/path/to/file.csv",
            file_hash="abc123",
            model="res.partner",
            config_hash="def456",
            last_completed_batch=5,
            total_batches=10,
            records_processed=100,
            records_created=95,
            records_failed=5,
        )
        assert cp.id_map == {}
        assert cp.deferred_fields == []
        assert cp.pass_1_complete is False
        assert cp.pass_2_complete is False
        assert cp.timestamp != ""


class TestFileHash:
    """Tests for file hash computation."""

    def test_compute_file_hash_returns_hash(self, sample_csv: str) -> None:
        """Test that file hash is computed correctly."""
        file_hash = ckpt._compute_file_hash(sample_csv)
        assert len(file_hash) == 16
        assert isinstance(file_hash, str)

    def test_compute_file_hash_consistent(self, sample_csv: str) -> None:
        """Test that same file produces same hash."""
        hash1 = ckpt._compute_file_hash(sample_csv)
        hash2 = ckpt._compute_file_hash(sample_csv)
        assert hash1 == hash2

    def test_compute_file_hash_nonexistent_file(self) -> None:
        """Test that nonexistent file returns 'unknown'."""
        file_hash = ckpt._compute_file_hash("/nonexistent/file.csv")
        assert file_hash == "unknown"


class TestSessionId:
    """Tests for session ID generation."""

    def test_generate_session_id_consistent(self, sample_csv: str) -> None:
        """Test that same inputs produce same session ID."""
        id1 = ckpt.generate_session_id(sample_csv, "config.conf", "res.partner")
        id2 = ckpt.generate_session_id(sample_csv, "config.conf", "res.partner")
        assert id1 == id2
        assert len(id1) == 32

    def test_generate_session_id_different_model(self, sample_csv: str) -> None:
        """Test that different model produces different ID."""
        id1 = ckpt.generate_session_id(sample_csv, "config.conf", "res.partner")
        id2 = ckpt.generate_session_id(sample_csv, "config.conf", "res.users")
        assert id1 != id2

    def test_generate_session_id_different_config(self, sample_csv: str) -> None:
        """Test that different config produces different ID."""
        id1 = ckpt.generate_session_id(sample_csv, "config1.conf", "res.partner")
        id2 = ckpt.generate_session_id(sample_csv, "config2.conf", "res.partner")
        assert id1 != id2

    def test_generate_session_id_with_dict_config(self, sample_csv: str) -> None:
        """Test session ID generation with dict config."""
        config = {"host": "localhost", "database": "test"}
        session_id = ckpt.generate_session_id(sample_csv, config, "res.partner")
        assert len(session_id) == 32


class TestCheckpointPaths:
    """Tests for checkpoint path utilities."""

    def test_get_checkpoint_dir(self, sample_csv: str) -> None:
        """Test checkpoint directory path."""
        cp_dir = ckpt.get_checkpoint_dir(sample_csv)
        assert cp_dir.name == ".odf_checkpoint"
        assert str(cp_dir.parent) == os.path.dirname(sample_csv)

    def test_get_checkpoint_path(self, sample_csv: str) -> None:
        """Test checkpoint file path."""
        session_id = "abc123"
        cp_path = ckpt.get_checkpoint_path(sample_csv, session_id)
        assert cp_path.name == "abc123.json"


class TestSaveLoadCheckpoint:
    """Tests for checkpoint save/load operations."""

    def test_save_and_load_checkpoint(self, sample_csv: str) -> None:
        """Test saving and loading a checkpoint."""
        session_id = ckpt.generate_session_id(sample_csv, "config.conf", "res.partner")
        file_hash = ckpt._compute_file_hash(sample_csv)

        # Create checkpoint
        cp = ckpt.CheckpointData(
            session_id=session_id,
            file_path=sample_csv,
            file_hash=file_hash,
            model="res.partner",
            config_hash="config_hash",
            last_completed_batch=5,
            total_batches=10,
            records_processed=100,
            records_created=95,
            records_failed=5,
            id_map={"ext_id_1": 1, "ext_id_2": 2},
            deferred_fields=["parent_id"],
            pass_1_complete=True,
            pass_2_complete=False,
        )

        # Save
        result = ckpt.save_checkpoint(cp)
        assert result is True

        # Load
        loaded = ckpt.load_checkpoint(sample_csv, "config.conf", "res.partner")
        assert loaded is not None
        assert loaded.session_id == session_id
        assert loaded.records_processed == 100
        assert loaded.id_map == {"ext_id_1": 1, "ext_id_2": 2}
        assert loaded.pass_1_complete is True

    def test_load_checkpoint_not_found(self, sample_csv: str) -> None:
        """Test loading nonexistent checkpoint returns None."""
        loaded = ckpt.load_checkpoint(sample_csv, "config.conf", "res.partner")
        assert loaded is None

    def test_load_checkpoint_file_changed(self, sample_csv: str) -> None:
        """Test that changed file invalidates checkpoint."""
        session_id = ckpt.generate_session_id(sample_csv, "config.conf", "res.partner")

        # Create checkpoint with original file hash
        cp = ckpt.CheckpointData(
            session_id=session_id,
            file_path=sample_csv,
            file_hash="original_hash",  # Different from actual file
            model="res.partner",
            config_hash="config_hash",
            last_completed_batch=5,
            total_batches=10,
            records_processed=100,
            records_created=95,
            records_failed=5,
        )
        ckpt.save_checkpoint(cp)

        # Load should fail because file hash doesn't match
        loaded = ckpt.load_checkpoint(sample_csv, "config.conf", "res.partner")
        assert loaded is None


class TestDeleteCheckpoint:
    """Tests for checkpoint deletion."""

    def test_delete_checkpoint(self, sample_csv: str) -> None:
        """Test deleting a checkpoint."""
        session_id = ckpt.generate_session_id(sample_csv, "config.conf", "res.partner")
        file_hash = ckpt._compute_file_hash(sample_csv)

        # Create and save checkpoint
        cp = ckpt.CheckpointData(
            session_id=session_id,
            file_path=sample_csv,
            file_hash=file_hash,
            model="res.partner",
            config_hash="config_hash",
            last_completed_batch=0,
            total_batches=1,
            records_processed=0,
            records_created=0,
            records_failed=0,
        )
        ckpt.save_checkpoint(cp)

        # Verify it exists
        cp_path = ckpt.get_checkpoint_path(sample_csv, session_id)
        assert cp_path.exists()

        # Delete
        result = ckpt.delete_checkpoint(sample_csv, session_id)
        assert result is True
        assert not cp_path.exists()

    def test_delete_nonexistent_checkpoint(self, sample_csv: str) -> None:
        """Test deleting nonexistent checkpoint succeeds."""
        result = ckpt.delete_checkpoint(sample_csv, "nonexistent")
        assert result is True


class TestCleanupOldCheckpoints:
    """Tests for checkpoint cleanup."""

    def test_cleanup_old_checkpoints(self, sample_csv: str) -> None:
        """Test cleaning up old checkpoints."""
        # Create checkpoint directory
        cp_dir = ckpt.get_checkpoint_dir(sample_csv)
        cp_dir.mkdir(parents=True, exist_ok=True)

        # Create an old checkpoint file with ancient timestamp
        old_cp_path = cp_dir / "old_session.json"
        old_data = {
            "session_id": "old_session",
            "timestamp": "2020-01-01T00:00:00",
            "file_hash": "test",
        }
        old_cp_path.write_text(json.dumps(old_data))

        # Cleanup
        deleted = ckpt.cleanup_old_checkpoints(sample_csv, max_age_days=7)
        assert deleted == 1
        assert not old_cp_path.exists()

    def test_cleanup_preserves_recent_checkpoints(self, sample_csv: str) -> None:
        """Test that recent checkpoints are preserved."""
        session_id = ckpt.generate_session_id(sample_csv, "config.conf", "res.partner")
        file_hash = ckpt._compute_file_hash(sample_csv)

        # Create a recent checkpoint
        cp = ckpt.CheckpointData(
            session_id=session_id,
            file_path=sample_csv,
            file_hash=file_hash,
            model="res.partner",
            config_hash="config_hash",
            last_completed_batch=0,
            total_batches=1,
            records_processed=0,
            records_created=0,
            records_failed=0,
        )
        ckpt.save_checkpoint(cp)

        # Cleanup should not delete it
        deleted = ckpt.cleanup_old_checkpoints(sample_csv, max_age_days=7)
        assert deleted == 0

        # Verify it still exists
        loaded = ckpt.load_checkpoint(sample_csv, "config.conf", "res.partner")
        assert loaded is not None

    def test_cleanup_no_checkpoint_dir(self, temp_dir: str) -> None:
        """Test cleanup when checkpoint directory doesn't exist."""
        nonexistent_csv = Path(temp_dir) / "nonexistent.csv"
        deleted = ckpt.cleanup_old_checkpoints(str(nonexistent_csv))
        assert deleted == 0

    def test_cleanup_corrupted_checkpoint_file(self, sample_csv: str) -> None:
        """Test that corrupted checkpoint files are deleted during cleanup."""
        cp_dir = ckpt.get_checkpoint_dir(sample_csv)
        cp_dir.mkdir(parents=True, exist_ok=True)

        # Create a corrupted checkpoint file
        corrupted_path = cp_dir / "corrupted.json"
        corrupted_path.write_text("this is not valid json {{{")

        deleted = ckpt.cleanup_old_checkpoints(sample_csv, max_age_days=7)
        assert deleted == 1
        assert not corrupted_path.exists()


class TestFileHashLargeFile:
    """Tests for file hash computation with large files."""

    def test_compute_file_hash_large_file(self, temp_dir: str) -> None:
        """Test file hash computation for files larger than 2MB."""
        large_file = Path(temp_dir) / "large_file.csv"
        # Create a file larger than 2MB (the threshold for reading last 1MB)
        content = "a" * (3 * 1024 * 1024)  # 3MB
        large_file.write_text(content)

        file_hash = ckpt._compute_file_hash(str(large_file))
        assert len(file_hash) == 16
        assert file_hash != "unknown"


class TestConfigHash:
    """Tests for config hash computation."""

    def test_compute_config_hash_with_non_dict_non_str(self) -> None:
        """Test config hash with object that is neither dict nor str."""

        # Pass an object like a dataclass or custom class
        class CustomConfig:
            def __str__(self) -> str:
                return "custom_config_value"

        config = CustomConfig()
        config_hash = ckpt._compute_config_hash(config)
        assert len(config_hash) == 16


class TestSaveCheckpointEdgeCases:
    """Tests for save_checkpoint edge cases."""

    def test_save_checkpoint_permission_error(self, sample_csv: str) -> None:
        """Test that save_checkpoint returns False on write error."""
        from unittest.mock import patch

        cp = ckpt.CheckpointData(
            session_id="test",
            file_path=sample_csv,
            file_hash="hash",
            model="res.partner",
            config_hash="config",
            last_completed_batch=0,
            total_batches=1,
            records_processed=0,
            records_created=0,
            records_failed=0,
        )

        with patch("builtins.open", side_effect=PermissionError("Permission denied")):
            result = ckpt.save_checkpoint(cp)
            assert result is False


class TestLoadCheckpointEdgeCases:
    """Tests for load_checkpoint edge cases."""

    def test_load_checkpoint_json_decode_error(self, sample_csv: str) -> None:
        """Test that corrupted JSON returns None."""
        session_id = ckpt.generate_session_id(sample_csv, "config.conf", "res.partner")

        # Create checkpoint directory and write corrupted file
        cp_dir = ckpt.get_checkpoint_dir(sample_csv)
        cp_dir.mkdir(parents=True, exist_ok=True)
        cp_path = ckpt.get_checkpoint_path(sample_csv, session_id)
        cp_path.write_text("this is not valid json {{{")

        loaded = ckpt.load_checkpoint(sample_csv, "config.conf", "res.partner")
        assert loaded is None

    def test_load_checkpoint_generic_exception(self, sample_csv: str) -> None:
        """Test that generic exceptions return None."""
        from unittest.mock import patch

        session_id = ckpt.generate_session_id(sample_csv, "config.conf", "res.partner")

        # Create a valid checkpoint first
        cp_dir = ckpt.get_checkpoint_dir(sample_csv)
        cp_dir.mkdir(parents=True, exist_ok=True)
        cp_path = ckpt.get_checkpoint_path(sample_csv, session_id)
        cp_path.write_text('{"valid": "json"}')

        with patch("builtins.open", side_effect=OSError("Read error")):
            loaded = ckpt.load_checkpoint(sample_csv, "config.conf", "res.partner")
            assert loaded is None


class TestDeleteCheckpointEdgeCases:
    """Tests for delete_checkpoint edge cases."""

    def test_delete_checkpoint_permission_error(self, sample_csv: str) -> None:
        """Test that delete_checkpoint returns False on permission error."""
        from unittest.mock import patch

        session_id = ckpt.generate_session_id(sample_csv, "config.conf", "res.partner")

        # Create checkpoint directory and file
        cp_dir = ckpt.get_checkpoint_dir(sample_csv)
        cp_dir.mkdir(parents=True, exist_ok=True)
        cp_path = ckpt.get_checkpoint_path(sample_csv, session_id)
        cp_path.write_text("{}")

        with patch.object(
            Path, "unlink", side_effect=PermissionError("Permission denied")
        ):
            result = ckpt.delete_checkpoint(sample_csv, session_id)
            assert result is False

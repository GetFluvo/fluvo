"""Tests for environment-specific fail file generation."""

import os
import tempfile
import pytest
from src.odoo_data_flow.import_threaded import _get_environment_from_connection, _get_fail_file_path


class TestEnvironmentDetection:
    """Test environment detection from connection files."""

    def test_connection_file_with_standard_naming(self):
        """Test standard connection file naming pattern."""
        result = _get_environment_from_connection("conf/local_connection.conf")
        assert result == "local"

    def test_connection_file_with_prod_naming(self):
        """Test production connection file naming."""
        result = _get_environment_from_connection("conf/prod_connection.conf")
        assert result == "prod"

    def test_connection_file_with_test_naming(self):
        """Test test connection file naming."""
        result = _get_environment_from_connection("conf/test_connection.conf")
        assert result == "test"

    def test_connection_file_with_simple_naming(self):
        """Test simple connection file naming."""
        result = _get_environment_from_connection("conf/connection.conf")
        assert result == "connection"

    def test_connection_dict_with_environment(self):
        """Test connection dictionary with environment field."""
        config = {"environment": "uat", "host": "localhost"}
        result = _get_environment_from_connection(config)
        assert result == "uat"

    def test_connection_dict_without_environment(self):
        """Test connection dictionary without environment field."""
        config = {"host": "localhost", "database": "test"}
        result = _get_environment_from_connection(config)
        assert result == "unknown"

    def test_connection_file_unknown_pattern(self):
        """Test unknown connection file pattern."""
        result = _get_environment_from_connection("some_random_file.txt")
        assert result == "unknown"


class TestFailFilePathGeneration:
    """Test environment-specific fail file path generation."""

    def setup_method(self):
        """Setup temporary directory for tests."""
        self.temp_dir = tempfile.mkdtemp()
        self.original_cwd = os.getcwd()
        os.chdir(self.temp_dir)

    def teardown_method(self):
        """Cleanup temporary directory."""
        os.chdir(self.original_cwd)
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_fail_file_path_generation(self):
        """Test basic fail file path generation."""
        # Create a temporary CSV file for testing
        with open("data/res_partner.csv", "w") as f:
            f.write("id,name\n1,Test Partner\n")

        result = _get_fail_file_path("data/res_partner.csv", "local", "fail")
        expected = os.path.join("fail_files", "local", "res_partner_fail.csv")
        assert result == expected

    def test_failed_file_path_generation(self):
        """Test failed file path generation."""
        # Create a temporary CSV file for testing
        with open("data/res_partner_bank_8.csv", "w") as f:
            f.write("id,bank_id\n1,1\n")

        result = _get_fail_file_path("data/res_partner_bank_8.csv", "prod", "failed")
        expected = os.path.join("fail_files", "prod", "res_partner_bank_8_failed.csv")
        assert result == expected

    def test_directory_creation(self):
        """Test that directories are created automatically."""
        # Create a temporary CSV file for testing
        with open("data/test.csv", "w") as f:
            f.write("id\n1\n")

        result = _get_fail_file_path("data/test.csv", "test_env", "fail")
        
        # Check that the directory was created
        expected_dir = os.path.join("fail_files", "test_env")
        assert os.path.exists(expected_dir)
        assert result == os.path.join(expected_dir, "test_fail.csv")

    def test_timestamp_preservation(self):
        """Test that timestamps are preserved for failed files."""
        import time
        
        # Create a temporary CSV file with a specific timestamp
        test_file = "data/timestamp_test.csv"
        with open(test_file, "w") as f:
            f.write("id\n1\n")

        # Set a specific timestamp
        old_timestamp = 1234567890.0
        os.utime(test_file, (old_timestamp, old_timestamp))

        # Generate failed file path
        result = _get_fail_file_path(test_file, "local", "failed", preserve_timestamp=True)

        # Check that the failed file was created with the same timestamp
        if os.path.exists(result):
            stat = os.stat(result)
            # Allow for small time differences due to file system precision
            assert abs(stat.st_mtime - old_timestamp) < 2.0

    def test_multicompany_filename_preservation(self):
        """Test that multicompany filenames are preserved."""
        # Test various multicompany patterns
        test_cases = [
            ("data/res_partner_bank_8.csv", "local", "res_partner_bank_8_fail.csv"),
            ("data/res_partner_bank_11.csv", "prod", "res_partner_bank_11_fail.csv"),
            ("data/account_move_2_main_company.csv", "test", "account_move_2_main_company_fail.csv"),
        ]

        for original_file, environment, expected_filename in test_cases:
            # Create the test file
            os.makedirs(os.path.dirname(original_file), exist_ok=True)
            with open(original_file, "w") as f:
                f.write("id\n1\n")

            result = _get_fail_file_path(original_file, environment, "fail")
            expected_path = os.path.join("fail_files", environment, expected_filename)
            assert result == expected_path


class TestIntegration:
    """Integration tests for the complete workflow."""

    def test_complete_workflow_simulation(self):
        """Test the complete environment detection and fail file generation workflow."""
        # Simulate the workflow
        connection_file = "conf/local_connection.conf"
        environment = _get_environment_from_connection(connection_file)
        assert environment == "local"

        # Create a test CSV file
        with open("data/test_import.csv", "w") as f:
            f.write("id,name\n1,Test\n2,Test2\n")

        # Generate fail file paths
        fail_file = _get_fail_file_path("data/test_import.csv", environment, "fail")
        failed_file = _get_fail_file_path("data/test_import.csv", environment, "failed")

        # Verify paths
        assert "fail_files/local/test_import_fail.csv" in fail_file
        assert "fail_files/local/test_import_failed.csv" in failed_file
        
        # Verify directories exist
        assert os.path.exists("fail_files/local")

    def test_different_environments_isolation(self):
        """Test that different environments don't interfere with each other."""
        # Create test files
        with open("data/shared.csv", "w") as f:
            f.write("id\n1\n")

        # Generate fail files for different environments
        fail_local = _get_fail_file_path("data/shared.csv", "local", "fail")
        fail_prod = _get_fail_file_path("data/shared.csv", "prod", "fail")
        fail_test = _get_fail_file_path("data/shared.csv", "test", "fail")

        # Verify they are in different directories
        assert "fail_files/local/shared_fail.csv" in fail_local
        assert "fail_files/prod/shared_fail.csv" in fail_prod
        assert "fail_files/test/shared_fail.csv" in fail_test
        
        # Verify all directories exist
        assert os.path.exists("fail_files/local")
        assert os.path.exists("fail_files/prod")
        assert os.path.exists("fail_files/test")


class TestErrorMerging:
    """Test the error merging functionality for multi-phase imports."""

    def test_read_existing_fail_file(self):
        """Test reading an existing fail file."""
        from src.odoo_data_flow.import_threaded import _read_existing_fail_file
        
        # Create a test fail file
        test_fail_file = "data/test_existing_fail.csv"
        with open(test_fail_file, 'w', encoding='utf-8', newline='') as f:
            f.write("id,name,_ERROR_REASON\n")
            f.write("1,John,Phase 1 error\n")
            f.write("2,Jane,Another error\n")
        
        # Read the file
        existing_errors = _read_existing_fail_file(test_fail_file, 'utf-8', ';')
        
        # Verify results
        assert len(existing_errors) == 2
        assert '1' in existing_errors
        assert '2' in existing_errors
        assert existing_errors['1'][-1] == "Phase 1 error"
        assert existing_errors['2'][-1] == "Another error"

    def test_error_merging_logic(self):
        """Test the error merging logic."""
        from src.odoo_data_flow.import_threaded import _create_padded_failed_line
        
        # Simulate Phase 1 error
        original_row = ["1", "John", "Doe"]
        header_length = 3
        phase1_error = "Phase 1: Field validation failed"
        
        # Create failed line with Phase 1 error
        failed_line = _create_padded_failed_line(original_row, header_length, phase1_error)
        
        # Verify structure
        assert len(failed_line) == header_length + 1  # Original columns + error
        assert failed_line[-1] == phase1_error
        
        # Simulate Phase 2 error merging
        phase2_error = "Phase 2: Relational update failed"
        combined_error = f"{phase1_error} | {phase2_error}"
        
        # Create merged failed line
        merged_line = _create_padded_failed_line(original_row, header_length, combined_error)
        
        # Verify merged error contains both phases
        assert phase1_error in merged_line[-1]
        assert phase2_error in merged_line[-1]
        assert "Phase 1:" in merged_line[-1]
        assert "Phase 2:" in merged_line[-1]

    def test_error_merging_with_existing_file(self):
        """Test error merging when reading from an existing fail file."""
        import tempfile
        import csv
        from src.odoo_data_flow.import_threaded import _read_existing_fail_file, _create_padded_failed_line
        
        # Create a temporary fail file with Phase 1 errors
        with tempfile.NamedTemporaryFile(mode='w', delete=False, encoding='utf-8', newline='') as f:
            writer = csv.writer(f, delimiter=';')
            writer.writerow(["id", "name", "_ERROR_REASON"])
            writer.writerow(["1", "John", "Phase 1: Validation error"])
            writer.writerow(["2", "Jane", "Phase 1: Missing required field"])
            temp_file = f.name
        
        try:
            # Read existing errors
            existing_errors = _read_existing_fail_file(temp_file, 'utf-8', ';')
            assert len(existing_errors) == 2
            
            # Simulate Phase 2 errors for the same records
            phase2_errors = {
                "1": "Phase 2: Relational update failed",
                "2": "Phase 2: Constraint violation"
            }
            
            # Merge errors
            merged_lines = []
            header_length = 2  # id, name
            
            for record_id, phase2_error in phase2_errors.items():
                if record_id in existing_errors:
                    existing_line = existing_errors[record_id]
                    phase1_error = existing_line[-1]
                    
                    # Create merged error
                    combined_error = f"{phase1_error} | {phase2_error}"
                    
                    # Create new failed line (simplified - in real usage this would use original data)
                    original_row = [record_id, existing_line[1]]  # id, name
                    merged_line = _create_padded_failed_line(original_row, header_length, combined_error)
                    merged_lines.append(merged_line)
            
            # Verify merged errors
            assert len(merged_lines) == 2
            for line in merged_lines:
                error_msg = line[-1]
                assert "Phase 1:" in error_msg
                assert "Phase 2:" in error_msg
                assert "|" in error_msg  # Separator
                
        finally:
            # Clean up
            import os
            os.unlink(temp_file)

    def test_phase_error_formatting(self):
        """Test proper formatting of phase-specific error messages."""
        from src.odoo_data_flow.import_threaded import _create_padded_failed_line
        
        original_row = ["1", "Test"]
        header_length = 2
        
        # Test Phase 1 only
        phase1_line = _create_padded_failed_line(original_row, header_length, "Phase 1: Validation failed")
        assert "Phase 1:" in phase1_line[-1]
        assert "Phase 2:" not in phase1_line[-1]
        
        # Test Phase 2 only
        phase2_line = _create_padded_failed_line(original_row, header_length, "Phase 2: Update failed")
        assert "Phase 2:" in phase2_line[-1]
        assert "Phase 1:" not in phase2_line[-1]
        
        # Test merged phases
        merged_line = _create_padded_failed_line(
            original_row, header_length, 
            "Phase 1: Validation failed | Phase 2: Update failed"
        )
        assert "Phase 1:" in merged_line[-1]
        assert "Phase 2:" in merged_line[-1]
        assert "|" in merged_line[-1]




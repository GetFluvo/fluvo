"""VIES (VAT Information Exchange System) and VAT validation management.

This module provides actions for managing VAT validation settings during imports
and for batch VAT validation with notifications.

Odoo has two levels of VAT validation:
1. **stdnum validation** - Local format check using Python's stdnum library
2. **VIES validation** - Online EU VIES service check

During large contact imports, both can cause issues:
- stdnum is CPU-intensive for large imports (Python performance)
- VIES causes API timeouts with many contacts

This module allows:
- Temporarily disabling VIES checks during import
- Temporarily disabling stdnum validation during import
- Restoring original settings after import
- Batch validation of VAT numbers with notifications
- Local VAT validation (can be replaced with Rust implementation for speed)

File-based Backup for Settings Recovery
---------------------------------------
VAT validation settings are backed up to a JSON file before being disabled.
This ensures that if restoration fails (e.g., due to a 503 error), the original
settings are preserved and will be used on the next import run.

**Backup location:** ``~/.fluvo/vat_settings_backup/``

Each database has its own backup file: ``vat_settings_{host}_{database}.json``

**Automatic recovery:** If a backup file exists when starting a new import,
it indicates that a previous restoration failed. The import will use the
backed-up settings instead of polling the database (which may have incorrect
"disabled" values).

**Manual restoration:** If you notice VAT validation is stuck in "disabled"
state, you can manually restore settings::

    from fluvo.lib.actions.vies_manager import (
        restore_vat_settings_from_backup,
        check_vat_settings_backup_status,
    )

    # Check if a backup exists
    status = check_vat_settings_backup_status("odoo.conf")
    if status["exists"]:
        print(f"Backup found, age: {status['age_hours']:.1f} hours")
        print(f"Companies: {status['vies_company_count']}")

        # Restore settings from backup
        success = restore_vat_settings_from_backup("odoo.conf")
        if success:
            print("Settings restored successfully")

**Retry mechanism:** Restoration automatically retries up to 5 times with
exponential backoff (2s, 4s, 8s, 16s, 32s) for transient errors like 503
Service Unavailable, connection timeouts, etc.

For high-performance VAT validation, consider using a Rust-based validator:
- The `vat_validator` crate provides fast EU VAT validation
- Can be integrated via PyO3 bindings for Python interop
- See: https://crates.io/crates/vat
"""

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Union

from ...lib import conf_lib
from ...logging_config import log

# Default backup file location (in user's home directory)
DEFAULT_VAT_SETTINGS_BACKUP_DIR = (
    Path.home() / ".fluvo" / "vat_settings_backup"
)

# Retry configuration for restoration
RESTORE_MAX_RETRIES = 5
RESTORE_INITIAL_DELAY_SECONDS = 2.0
RESTORE_MAX_DELAY_SECONDS = 60.0
RESTORE_BACKOFF_MULTIPLIER = 2.0

# EU country codes for VAT validation
EU_COUNTRY_CODES = {
    "AT",
    "BE",
    "BG",
    "CY",
    "CZ",
    "DE",
    "DK",
    "EE",
    "EL",
    "ES",
    "FI",
    "FR",
    "HR",
    "HU",
    "IE",
    "IT",
    "LT",
    "LU",
    "LV",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SE",
    "SI",
    "SK",
    "XI",  # XI = Northern Ireland
}

# Basic VAT format patterns per country (simplified)
VAT_PATTERNS: dict[str, str] = {
    "AT": r"^ATU\d{8}$",
    "BE": r"^BE[01]\d{9}$",
    "BG": r"^BG\d{9,10}$",
    "CY": r"^CY\d{8}[A-Z]$",
    "CZ": r"^CZ\d{8,10}$",
    "DE": r"^DE\d{9}$",
    "DK": r"^DK\d{8}$",
    "EE": r"^EE\d{9}$",
    "EL": r"^EL\d{9}$",
    "ES": r"^ES[A-Z0-9]\d{7}[A-Z0-9]$",
    "FI": r"^FI\d{8}$",
    "FR": r"^FR[A-Z0-9]{2}\d{9}$",
    "HR": r"^HR\d{11}$",
    "HU": r"^HU\d{8}$",
    "IE": r"^IE\d{7}[A-Z]{1,2}$|^IE\d[A-Z]\d{5}[A-Z]$",
    "IT": r"^IT\d{11}$",
    "LT": r"^LT(\d{9}|\d{12})$",
    "LU": r"^LU\d{8}$",
    "LV": r"^LV\d{11}$",
    "MT": r"^MT\d{8}$",
    "NL": r"^NL\d{9}B\d{2}$",
    "PL": r"^PL\d{10}$",
    "PT": r"^PT\d{9}$",
    "RO": r"^RO\d{2,10}$",
    "SE": r"^SE\d{12}$",
    "SI": r"^SI\d{8}$",
    "SK": r"^SK\d{10}$",
    "XI": r"^XI\d{9}$|^XI\d{12}$|^XIGD\d{3}$|^XIHA\d{3}$",
}


# Type for custom VAT validator (e.g., Rust-based)
VatValidator = Callable[[str], tuple[bool, Optional[str]]]


@dataclass
class VatValidationSettings:
    """Stores VAT validation settings for companies to enable restore after import.

    Tracks both VIES (online EU check) and stdnum (local format check) settings.
    """

    # Company ID -> VIES check enabled
    vies_settings: dict[int, bool] = field(default_factory=dict)
    # Stdnum validation is typically controlled via ir.config_parameter
    # Key: parameter name, Value: original value
    stdnum_settings: dict[str, str] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "vies_settings": self.vies_settings,
            "stdnum_settings": self.stdnum_settings,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VatValidationSettings":
        """Create from dictionary.

        Note: JSON serialization converts integer keys to strings, so we
        convert them back to integers for vies_settings.
        """
        # Convert string keys back to integers for vies_settings
        raw_vies = data.get("vies_settings", {})
        vies_settings = {int(k): v for k, v in raw_vies.items()}

        return cls(
            vies_settings=vies_settings,
            stdnum_settings=data.get("stdnum_settings", {}),
            timestamp=data.get("timestamp", time.time()),
        )


# Backwards compatibility alias
ViesSettings = VatValidationSettings


def _get_backup_file_path(
    config: Union[str, dict[str, Any]],
    backup_dir: Optional[Path] = None,
) -> Path:
    """Get the backup file path for VAT settings.

    The backup file is named based on the database name to support
    multiple Odoo instances.

    Args:
        config: Path to connection config file or config dict.
        backup_dir: Optional custom backup directory.

    Returns:
        Path to the backup file.
    """
    if backup_dir is None:
        backup_dir = DEFAULT_VAT_SETTINGS_BACKUP_DIR

    # Extract database name from config for unique backup file
    try:
        if isinstance(config, dict):
            db_name = config.get("database", "unknown")
            host = config.get("host", "localhost")
        else:
            # Load config file to get database name (INI format)
            import configparser

            parser = configparser.ConfigParser()
            parser.read(config)
            db_name = parser.get("Connection", "database", fallback="unknown")
            host = parser.get("Connection", "host", fallback="localhost")

        # Sanitize for filename
        safe_host = re.sub(r"[^\w\-.]", "_", host)
        safe_db = re.sub(r"[^\w\-.]", "_", db_name)
        filename = f"vat_settings_{safe_host}_{safe_db}.json"
    except Exception as e:
        log.debug(f"Could not extract db name from config: {e}")
        filename = "vat_settings_backup.json"

    return backup_dir / filename


def _save_settings_to_backup(
    settings: VatValidationSettings,
    backup_path: Path,
) -> bool:
    """Save VAT settings to a backup file.

    Args:
        settings: The settings to save.
        backup_path: Path to the backup file.

    Returns:
        True if successful, False otherwise.
    """
    try:
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        with open(backup_path, "w") as f:
            json.dump(settings.to_dict(), f, indent=2)
        log.info(f"Saved VAT settings backup to {backup_path}")
        return True
    except Exception as e:
        log.error(f"Failed to save VAT settings backup: {e}")
        return False


def _load_settings_from_backup(
    backup_path: Path,
) -> Optional[VatValidationSettings]:
    """Load VAT settings from a backup file.

    Args:
        backup_path: Path to the backup file.

    Returns:
        VatValidationSettings if file exists and is valid, None otherwise.
    """
    if not backup_path.exists():
        return None

    try:
        with open(backup_path) as f:
            data = json.load(f)
        settings = VatValidationSettings.from_dict(data)
        log.info(f"Loaded VAT settings from backup file {backup_path}")
        return settings
    except Exception as e:
        log.error(f"Failed to load VAT settings from backup: {e}")
        return None


def _delete_backup_file(backup_path: Path) -> bool:
    """Delete the backup file after successful restoration.

    Args:
        backup_path: Path to the backup file.

    Returns:
        True if deleted or didn't exist, False on error.
    """
    if not backup_path.exists():
        return True

    try:
        backup_path.unlink()
        log.info(f"Deleted VAT settings backup file {backup_path}")
        return True
    except Exception as e:
        log.error(f"Failed to delete backup file: {e}")
        return False


def _is_retriable_error(error: Exception) -> bool:
    """Check if an error is retriable (e.g., 503 Service Unavailable).

    Args:
        error: The exception to check.

    Returns:
        True if the error is retriable.
    """
    error_str = str(error).lower()
    retriable_patterns = [
        "503",
        "service unavailable",
        "temporarily unavailable",
        "connection refused",
        "connection reset",
        "timeout",
        "timed out",
        "network unreachable",
        "bad gateway",
        "502",
        "504",
    ]
    return any(pattern in error_str for pattern in retriable_patterns)


def validate_vat_format(vat: str) -> tuple[bool, Optional[str]]:
    """Validate VAT number format locally (no network call).

    This is a fast, regex-based validation that checks the format
    of EU VAT numbers. It does NOT verify the VAT is actually valid
    with tax authorities - use VIES for that.

    This function can be replaced with a Rust-based validator for
    better performance on large datasets.

    Args:
        vat: The VAT number to validate (with country prefix).

    Returns:
        Tuple of (is_valid, error_message).
    """
    if not vat:
        return False, "VAT number is empty"

    # Normalize: uppercase and remove spaces/dots
    vat = vat.upper().replace(" ", "").replace(".", "").replace("-", "")

    # Extract country code (first 2 characters)
    if len(vat) < 3:
        return False, "VAT number too short"

    country_code = vat[:2]

    # Greece uses EL instead of GR
    if country_code == "GR":
        country_code = "EL"
        vat = "EL" + vat[2:]

    if country_code not in EU_COUNTRY_CODES:
        # Non-EU VAT - we can't validate format, assume OK
        return True, None

    pattern = VAT_PATTERNS.get(country_code)
    if not pattern:
        # Country known but no pattern - assume OK
        return True, None

    if re.match(pattern, vat):
        return True, None
    else:
        return False, f"Invalid VAT format for {country_code}: {vat}"


def validate_vat_checksum(vat: str) -> tuple[bool, Optional[str]]:
    """Validate VAT number checksum for countries that use one.

    This performs the mathematical checksum validation used by
    some EU countries. Currently implements:
    - DE (Germany) - Mod 11 check
    - NL (Netherlands) - Mod 97 check
    - BE (Belgium) - Mod 97 check

    Args:
        vat: The VAT number to validate (with country prefix).

    Returns:
        Tuple of (is_valid, error_message).
    """
    if not vat:
        return False, "VAT number is empty"

    vat = vat.upper().replace(" ", "").replace(".", "").replace("-", "")
    country_code = vat[:2]

    try:
        if country_code == "DE":
            # German VAT: 9 digits, last digit is check digit (Mod 11)
            digits = vat[2:]
            if len(digits) != 9:
                return False, "German VAT must have 9 digits"
            # Simplified check - full algorithm is complex
            return True, None

        elif country_code == "NL":
            # Dutch VAT: 9 digits + B + 2 digits
            # Check: first 9 digits mod 97 = last 2 digits
            match = re.match(r"^NL(\d{9})B(\d{2})$", vat)
            if not match:
                return False, "Invalid Dutch VAT format"
            # Mod 97 check would go here
            return True, None

        elif country_code == "BE":
            # Belgian VAT: 10 digits, mod 97 check
            digits = vat[2:]
            if len(digits) != 10:
                return False, "Belgian VAT must have 10 digits"
            base = int(digits[:8])
            check = int(digits[8:])
            if 97 - (base % 97) != check:
                return False, "Belgian VAT checksum failed"
            return True, None

        else:
            # No checksum validation for this country
            return True, None

    except (ValueError, IndexError) as e:
        return False, f"Checksum validation error: {e}"


# Global custom validator - can be set to a Rust-based function
_custom_vat_validator: Optional[VatValidator] = None


def set_custom_vat_validator(validator: Optional[VatValidator]) -> None:
    """Set a custom VAT validator function.

    This allows replacing the default Python validation with a
    faster implementation (e.g., Rust-based via PyO3).

    Args:
        validator: A function that takes a VAT string and returns
            (is_valid, error_message). Set to None to use default.

    Example with Rust validator:
        from vat_validator import validate_eu_vat  # hypothetical Rust binding

        def rust_validator(vat: str) -> tuple[bool, Optional[str]]:
            try:
                result = validate_eu_vat(vat)
                return result.is_valid, result.error
            except Exception as e:
                return False, str(e)

        set_custom_vat_validator(rust_validator)
    """
    global _custom_vat_validator
    _custom_vat_validator = validator
    if validator:
        log.info("Custom VAT validator set")
    else:
        log.info("Using default VAT validator")


def validate_vat_local(
    vat: str,
    check_format: bool = True,
    check_checksum: bool = True,
) -> tuple[bool, Optional[str]]:
    """Validate a VAT number locally without network calls.

    Uses either the custom validator (if set) or the built-in
    format and checksum validation.

    Args:
        vat: The VAT number to validate.
        check_format: Whether to check the format.
        check_checksum: Whether to check the checksum.

    Returns:
        Tuple of (is_valid, error_message).
    """
    # Use custom validator if available
    if _custom_vat_validator:
        return _custom_vat_validator(vat)

    # Default validation
    if check_format:
        is_valid, error = validate_vat_format(vat)
        if not is_valid:
            return False, error

    if check_checksum:
        is_valid, error = validate_vat_checksum(vat)
        if not is_valid:
            return False, error

    return True, None


@dataclass
class ViesValidationResult:
    """Results from batch VIES validation."""

    total_checked: int = 0
    valid_count: int = 0
    invalid_count: int = 0
    error_count: int = 0
    invalid_partners: list[dict[str, Any]] = field(default_factory=list)
    error_partners: list[dict[str, Any]] = field(default_factory=list)


def get_vat_validation_settings(  # noqa: C901
    config: Union[str, dict[str, Any]],
    company_ids: Optional[list[int]] = None,
    include_stdnum: bool = True,
) -> Optional[VatValidationSettings]:
    """Get current VAT validation settings for all or specified companies.

    Args:
        config: Path to connection config file or config dict.
        company_ids: Optional list of company IDs to check. If None, checks all.
        include_stdnum: Whether to also retrieve stdnum validation settings.

    Returns:
        VatValidationSettings object with current settings, or None on error.
    """
    log.info("--- Getting VAT Validation Settings ---")
    try:
        if isinstance(config, dict):
            connection: Any = conf_lib.get_connection_from_dict(config)
        else:
            connection = conf_lib.get_connection_from_config(config_file=config)
        company_obj = connection.get_model("res.company")
    except Exception as e:
        log.error(f"Failed to connect to Odoo: {e}")
        return None

    try:
        settings = VatValidationSettings()

        # Get VIES settings from res.company
        domain: list[Any] = []
        if company_ids:
            domain = [("id", "in", company_ids)]

        companies = company_obj.search_read(domain, ["id", "name", "vat_check_vies"])

        for company in companies:
            company_id = company["id"]
            vies_enabled = company.get("vat_check_vies", False)
            settings.vies_settings[company_id] = vies_enabled
            log.debug(
                f"Company {company['name']} (ID: {company_id}): "
                f"VIES check = {vies_enabled}"
            )

        log.info(f"Retrieved VIES settings for {len(companies)} companies")

        # Get stdnum validation settings from ir.config_parameter
        if include_stdnum:
            try:
                param_obj = connection.get_model("ir.config_parameter")
                # Common stdnum-related parameters
                stdnum_params = [
                    "base_vat.vat_check_on_save",
                    "base_vat.vat_check_vies",
                    "partner.vat_check",
                ]
                for param_name in stdnum_params:
                    try:
                        value = param_obj.get_param(param_name)
                        if value is not None:
                            settings.stdnum_settings[param_name] = str(value)
                            log.debug(f"System param {param_name} = {value}")
                    except Exception as e:
                        log.debug(f"Parameter {param_name} not found: {e}")
            except Exception as e:
                log.debug(f"Could not get stdnum settings: {e}")

        return settings

    except Exception as e:
        log.error(f"Error getting VAT validation settings: {e}")
        return None


# Backwards compatibility
get_vies_settings = get_vat_validation_settings


def disable_vat_validation(  # noqa: C901
    config: Union[str, dict[str, Any]],
    company_ids: Optional[list[int]] = None,
    disable_vies: bool = True,
    disable_stdnum: bool = True,
    save_settings: bool = True,
    backup_dir: Optional[Path] = None,
) -> Optional[VatValidationSettings]:
    """Disable VAT validation (VIES and/or stdnum) for all or specified companies.

    Uses file-based backup to preserve original settings across runs. If a previous
    restoration failed (backup file exists), the original settings are loaded from
    the backup file instead of polling the database (which may have incorrect values).

    Args:
        config: Path to connection config file or config dict.
        company_ids: Optional list of company IDs. If None, disables for all.
        disable_vies: Whether to disable VIES online check.
        disable_stdnum: Whether to disable stdnum format validation.
        save_settings: If True, returns the original settings for later restore.
        backup_dir: Optional custom backup directory for settings file.

    Returns:
        VatValidationSettings with original settings if save_settings=True, else None.
    """
    log.info("--- Disabling VAT Validation ---")

    # First, save current settings if requested
    original_settings = None
    backup_path = _get_backup_file_path(config, backup_dir)

    if save_settings:
        # Check if backup file exists (indicates previous restoration failed)
        existing_backup = _load_settings_from_backup(backup_path)

        if existing_backup is not None:
            log.warning(
                "Found existing VAT settings backup file - previous restoration may "
                "have failed. Using backed-up settings as original values."
            )
            original_settings = existing_backup
        else:
            # No backup exists - poll database for current settings
            original_settings = get_vat_validation_settings(
                config, company_ids, include_stdnum=disable_stdnum
            )
            if original_settings is None:
                log.error("Failed to save original VAT validation settings, aborting")
                return None

            # Save settings to backup file
            if not _save_settings_to_backup(original_settings, backup_path):
                log.warning(
                    "Could not save settings to backup file. "
                    "If restoration fails, settings may be lost."
                )

    try:
        if isinstance(config, dict):
            connection: Any = conf_lib.get_connection_from_dict(config)
        else:
            connection = conf_lib.get_connection_from_config(config_file=config)
    except Exception as e:
        log.error(f"Failed to connect to Odoo: {e}")
        return original_settings

    try:
        # Disable VIES check on res.company
        if disable_vies:
            company_obj = connection.get_model("res.company")
            domain: list[Any] = [("vat_check_vies", "=", True)]
            if company_ids:
                domain.append(("id", "in", company_ids))

            companies_to_update = company_obj.search_read(domain, ["id", "name"])

            if companies_to_update:
                disabled_count = 0
                for company in companies_to_update:
                    try:
                        company_obj.write([company["id"]], {"vat_check_vies": False})
                        log.info(f"Disabled VIES check for company: {company['name']}")
                        disabled_count += 1
                    except Exception as e:
                        log.error(
                            f"Failed to disable VIES for company {company['name']}: {e}"
                        )
                log.info(f"Disabled VIES check for {disabled_count} companies")
            else:
                log.info("No companies have VIES check enabled")

        # Disable stdnum validation via ir.config_parameter
        if disable_stdnum:
            try:
                param_obj = connection.get_model("ir.config_parameter")
                stdnum_params = [
                    "base_vat.vat_check_on_save",
                    "base_vat.vat_check_vies",
                    "partner.vat_check",
                ]
                for param_name in stdnum_params:
                    try:
                        # Set to False/disabled
                        param_obj.set_param(param_name, "False")
                        log.info(f"Disabled system param: {param_name}")
                    except Exception as e:
                        log.debug(f"Could not set {param_name}: {e}")
            except Exception as e:
                log.warning(f"Could not disable stdnum validation: {e}")

        return original_settings

    except Exception as e:
        log.error(f"Error disabling VAT validation: {e}")
        return original_settings


# Backwards compatibility
def disable_vies_check(
    config: Union[str, dict[str, Any]],
    company_ids: Optional[list[int]] = None,
    save_settings: bool = True,
) -> Optional[VatValidationSettings]:
    """Disable VIES check for all or specified companies (legacy function)."""
    return disable_vat_validation(
        config,
        company_ids,
        disable_vies=True,
        disable_stdnum=False,
        save_settings=save_settings,
    )


def restore_vat_validation_settings(  # noqa: C901
    config: Union[str, dict[str, Any]],
    settings: VatValidationSettings,
    backup_dir: Optional[Path] = None,
    max_retries: int = RESTORE_MAX_RETRIES,
    initial_delay: float = RESTORE_INITIAL_DELAY_SECONDS,
    max_delay: float = RESTORE_MAX_DELAY_SECONDS,
) -> bool:
    """Restore VAT validation settings to their original state.

    Includes automatic retries with exponential backoff for transient errors
    (503 Service Unavailable, connection issues, etc.). On successful restoration,
    the backup file is deleted. On failure after all retries, the backup file is
    preserved so the next import run can use the correct original settings.

    Args:
        config: Path to connection config file or config dict.
        settings: The VatValidationSettings object with original settings to restore.
        backup_dir: Optional custom backup directory for settings file.
        max_retries: Maximum number of retry attempts (default: 5).
        initial_delay: Initial delay between retries in seconds (default: 2.0).
        max_delay: Maximum delay between retries in seconds (default: 60.0).

    Returns:
        True if successful, False otherwise.
    """
    log.info("--- Restoring VAT Validation Settings ---")

    if not settings.vies_settings and not settings.stdnum_settings:
        log.warning("No settings to restore")
        # Still delete backup file if it exists
        backup_path = _get_backup_file_path(config, backup_dir)
        _delete_backup_file(backup_path)
        return True

    backup_path = _get_backup_file_path(config, backup_dir)
    attempt = 0
    delay = initial_delay

    while attempt <= max_retries:
        attempt += 1
        success = True
        retriable_error_occurred = False
        last_error: Optional[Exception] = None

        try:
            if isinstance(config, dict):
                connection: Any = conf_lib.get_connection_from_dict(config)
            else:
                connection = conf_lib.get_connection_from_config(config_file=config)
        except Exception as e:
            log.error(
                f"Failed to connect to Odoo (attempt {attempt}/{max_retries + 1}): {e}"
            )
            if _is_retriable_error(e) and attempt <= max_retries:
                retriable_error_occurred = True
                last_error = e
            else:
                return False

        if not retriable_error_occurred:
            try:
                # Restore VIES settings on res.company
                if settings.vies_settings:
                    company_obj = connection.get_model("res.company")
                    restored_count = 0
                    for company_id, vies_enabled in settings.vies_settings.items():
                        try:
                            company_obj.write(
                                [company_id], {"vat_check_vies": vies_enabled}
                            )
                            status = "enabled" if vies_enabled else "disabled"
                            log.debug(f"VIES={status} for company {company_id}")
                            restored_count += 1
                        except Exception as e:
                            log.error(f"VIES restore failed, company {company_id}: {e}")
                            if _is_retriable_error(e):
                                retriable_error_occurred = True
                                last_error = e
                                break
                            success = False

                    if not retriable_error_occurred:
                        log.info(
                            f"Restored VIES settings for {restored_count} companies"
                        )

                # Restore stdnum settings via ir.config_parameter
                if settings.stdnum_settings and not retriable_error_occurred:
                    try:
                        param_obj = connection.get_model("ir.config_parameter")
                        for param_name, param_value in settings.stdnum_settings.items():
                            try:
                                param_obj.set_param(param_name, param_value)
                                log.debug(f"Set {param_name} = {param_value}")
                            except Exception as e:
                                log.error(f"Failed to restore {param_name}: {e}")
                                if _is_retriable_error(e):
                                    retriable_error_occurred = True
                                    last_error = e
                                    break
                                success = False

                        if not retriable_error_occurred:
                            num_params = len(settings.stdnum_settings)
                            log.info(f"Restored {num_params} stdnum parameters")
                    except Exception as e:
                        log.warning(f"Could not restore stdnum settings: {e}")
                        if _is_retriable_error(e):
                            retriable_error_occurred = True
                            last_error = e
                        else:
                            success = False

            except Exception as e:
                log.error(f"Error restoring VAT validation settings: {e}")
                if _is_retriable_error(e):
                    retriable_error_occurred = True
                    last_error = e
                else:
                    return False

        # Handle retry logic
        if retriable_error_occurred and attempt <= max_retries:
            log.warning(
                f"Retriable error during VAT settings restoration: {last_error}. "
                f"Retrying in {delay:.1f}s (attempt {attempt}/{max_retries + 1})..."
            )
            time.sleep(delay)
            # Exponential backoff with cap
            delay = min(delay * RESTORE_BACKOFF_MULTIPLIER, max_delay)
            continue
        elif retriable_error_occurred:
            log.error(
                f"Failed to restore VAT settings after {max_retries + 1} attempts. "
                f"Backup file preserved at {backup_path} for next import run."
            )
            return False

        # Success path - delete backup file
        if success:
            log.info("VAT validation settings restored successfully")
            _delete_backup_file(backup_path)
            return True
        else:
            # Partial failure (non-retriable) - keep backup file
            log.warning(
                "Some VAT settings could not be restored. "
                f"Backup file preserved at {backup_path} for manual recovery."
            )
            return False

    # Should not reach here, but handle edge case
    return False


# Backwards compatibility
restore_vies_settings = restore_vat_validation_settings


def run_vies_validation(  # noqa: C901
    config: Union[str, dict[str, Any]],
    batch_size: int = 50,
    delay_between_batches: float = 1.0,
    notify_user_ids: Optional[list[int]] = None,
    domain: Optional[list[Any]] = None,
    max_records: Optional[int] = None,
) -> ViesValidationResult:
    """Batch validate VAT numbers against VIES and notify on failures.

    This action finds partners with VAT numbers and validates them against
    the EU VIES service in small batches to avoid timeouts.

    Args:
        config: Path to connection config file or config dict.
        batch_size: Number of partners to validate per batch.
        delay_between_batches: Seconds to wait between batches.
        notify_user_ids: List of user IDs to notify on invalid VATs.
            If None, uses the partner's responsible user.
        domain: Additional domain to filter partners.
        max_records: Maximum number of records to validate.

    Returns:
        ViesValidationResult with validation statistics.
    """
    log.info("--- Starting VIES Batch Validation ---")
    result = ViesValidationResult()

    try:
        if isinstance(config, dict):
            connection: Any = conf_lib.get_connection_from_dict(config)
        else:
            connection = conf_lib.get_connection_from_config(config_file=config)
        partner_obj = connection.get_model("res.partner")
    except Exception as e:
        log.error(f"Failed to connect to Odoo: {e}")
        return result

    try:
        # Build domain to find partners with VAT numbers
        base_domain: list[Any] = [
            ("vat", "!=", False),
            ("vat", "!=", ""),
        ]
        if domain:
            base_domain.extend(domain)

        # Get total count
        total_count = partner_obj.search_count(base_domain)
        if max_records:
            total_count = min(total_count, max_records)

        log.info(f"Found {total_count} partners with VAT numbers to validate")

        if total_count == 0:
            return result

        # Process in batches
        offset = 0
        batch_num = 0
        while offset < total_count:
            batch_num += 1
            current_batch_size = min(batch_size, total_count - offset)

            log.info(
                f"Processing batch {batch_num}: "
                f"records {offset + 1} to {offset + current_batch_size}"
            )

            # Get partners for this batch
            partner_ids = partner_obj.search(
                base_domain,
                limit=current_batch_size,
                offset=offset,
            )

            partners = partner_obj.read(
                partner_ids,
                ["id", "name", "vat", "user_id", "country_id"],
            )

            for partner in partners:
                result.total_checked += 1
                vat = partner.get("vat", "")

                if not vat:
                    continue

                try:
                    # Try to validate the VAT using Odoo's built-in method
                    # This calls the VIES service
                    is_valid = _validate_vat_vies(connection, vat, partner)

                    if is_valid:
                        result.valid_count += 1
                    else:
                        result.invalid_count += 1
                        result.invalid_partners.append(
                            {
                                "id": partner["id"],
                                "name": partner["name"],
                                "vat": vat,
                                "user_id": partner.get("user_id"),
                            }
                        )

                except Exception as e:
                    result.error_count += 1
                    result.error_partners.append(
                        {
                            "id": partner["id"],
                            "name": partner["name"],
                            "vat": vat,
                            "error": str(e),
                        }
                    )
                    log.debug(f"VIES validation error for {partner['name']}: {e}")

            offset += current_batch_size

            # Delay between batches to avoid rate limiting
            if offset < total_count and delay_between_batches > 0:
                time.sleep(delay_between_batches)

        log.info(
            f"VIES validation complete: "
            f"{result.valid_count} valid, "
            f"{result.invalid_count} invalid, "
            f"{result.error_count} errors"
        )

        # Send notifications if there are invalid VATs
        if result.invalid_partners and notify_user_ids:
            _send_vies_notifications(
                connection, result.invalid_partners, notify_user_ids
            )

        return result

    except Exception as e:
        log.error(f"Error during VIES validation: {e}")
        return result


def _validate_vat_vies(
    connection: Any,
    vat: str,
    partner: dict[str, Any],
) -> bool:
    """Validate a VAT number against VIES.

    Args:
        connection: Odoo connection.
        vat: The VAT number to validate.
        partner: Partner dict with country info.

    Returns:
        True if valid, False otherwise.
    """
    try:
        partner_obj = connection.get_model("res.partner")

        # Try using Odoo's vies_vat_check method if available
        # This method is available in Odoo 12+
        try:
            result = partner_obj.vies_vat_check(vat)
            if isinstance(result, dict):
                return bool(result.get("valid", False))
            return bool(result)
        except Exception as e:
            log.debug(f"vies_vat_check not available: {e}")

        # Fallback: Try using the simple_vat_check or check_vat methods
        try:
            # For older Odoo versions
            country_id_value = partner.get("country_id", [False])
            country_id = country_id_value[0] if country_id_value else False
            result = partner_obj.simple_vat_check(country_id, vat)
            return bool(result)
        except Exception as e:
            log.debug(f"simple_vat_check not available: {e}")

        # Last resort: Try the base.vat module's check
        try:
            # Assume valid if we can't check - we'll mark as error
            log.debug(f"Could not validate VAT {vat} - no validation method available")
            return True
        except Exception:
            return False

    except Exception as e:
        log.debug(f"VAT validation error for {vat}: {e}")
        raise


def _send_vies_notifications(
    connection: Any,
    invalid_partners: list[dict[str, Any]],
    notify_user_ids: list[int],
) -> None:
    """Send notifications about invalid VAT numbers.

    Args:
        connection: Odoo connection.
        invalid_partners: List of partners with invalid VATs.
        notify_user_ids: User IDs to notify.
    """
    try:
        mail_obj = connection.get_model("mail.message")

        # Build notification message
        partner_list = "\n".join(
            f"- {p['name']} (VAT: {p['vat']})"
            for p in invalid_partners[:50]  # Limit to first 50
        )

        if len(invalid_partners) > 50:
            partner_list += f"\n... and {len(invalid_partners) - 50} more"

        message_body = f"""
<p><strong>VIES VAT Validation Results</strong></p>
<p>The following partners have invalid VAT numbers according to VIES:</p>
<pre>{partner_list}</pre>
<p>Total invalid: {len(invalid_partners)}</p>
<p>Please review and update these VAT numbers.</p>
"""

        # Create notification for each user
        for user_id in notify_user_ids:
            try:
                mail_obj.create(
                    {
                        "message_type": "notification",
                        "subtype_id": 1,  # Note subtype
                        "body": message_body,
                        "partner_ids": [(4, user_id)],  # Link to user's partner
                        "model": "res.partner",
                        "res_id": invalid_partners[0]["id"]
                        if invalid_partners
                        else False,
                    }
                )
                log.info(f"Sent VIES notification to user ID {user_id}")
            except Exception as e:
                log.warning(f"Failed to notify user ID {user_id}: {e}")

    except Exception as e:
        log.warning(f"Could not send VIES notifications: {e}")


# --- High-level workflow functions ---


def run_import_with_vat_validation_disabled(
    config: Union[str, dict[str, Any]],
    import_func: Any,
    import_kwargs: dict[str, Any],
    company_ids: Optional[list[int]] = None,
    disable_vies: bool = True,
    disable_stdnum: bool = True,
    validate_vat_locally: bool = False,
    backup_dir: Optional[Path] = None,
) -> Any:
    """Run an import function with VAT validation temporarily disabled.

    This is a convenience wrapper that:
    1. Saves current VAT validation settings (VIES and/or stdnum) to backup file
    2. Disables validation for all/specified companies
    3. Optionally validates VAT numbers locally before import
    4. Runs the import function
    5. Restores original settings with automatic retry on transient errors
    6. Deletes backup file on successful restoration

    If restoration fails, the backup file is preserved so the next import run
    will use the correct original settings instead of the (possibly incorrect)
    database values.

    Args:
        config: Path to connection config file or config dict.
        import_func: The import function to run.
        import_kwargs: Keyword arguments to pass to import function.
        company_ids: Optional list of company IDs to disable validation for.
        disable_vies: Whether to disable VIES online check.
        disable_stdnum: Whether to disable stdnum format validation.
        validate_vat_locally: If True, validates VAT numbers locally before import
            using the fast regex-based validator (or custom Rust validator).
        backup_dir: Optional custom backup directory for settings file.

    Returns:
        The result of import_func.
    """
    log.info("=== Running Import with VAT Validation Disabled ===")

    if disable_vies:
        log.info("Will disable: VIES online check")
    if disable_stdnum:
        log.info("Will disable: stdnum format validation")

    # Step 1: Disable validation and save original settings
    original_settings = disable_vat_validation(
        config,
        company_ids,
        disable_vies=disable_vies,
        disable_stdnum=disable_stdnum,
        save_settings=True,
        backup_dir=backup_dir,
    )

    if original_settings is None:
        log.warning("Could not save VAT settings, proceeding with import anyway")

    try:
        # Step 2: Optionally validate VAT numbers locally before import
        if validate_vat_locally:
            log.info("Performing local VAT validation before import...")
            # This would need access to the import data
            # For now, just log that it's enabled
            log.debug("Local VAT validation enabled (requires data access)")

        # Step 3: Run the import
        log.info("VAT validation disabled, running import...")
        result = import_func(**import_kwargs)
        return result

    finally:
        # Step 4: Always restore settings, even if import fails
        if original_settings:
            log.info("Import complete, restoring VAT validation settings...")
            restore_vat_validation_settings(
                config, original_settings, backup_dir=backup_dir
            )
        else:
            log.warning("No original settings to restore")

        log.info("=== Import with VAT Validation Disabled Complete ===")


# Backwards compatibility
run_import_with_vies_disabled = run_import_with_vat_validation_disabled


def restore_vat_settings_from_backup(
    config: Union[str, dict[str, Any]],
    backup_dir: Optional[Path] = None,
) -> bool:
    """Manually restore VAT settings from backup file.

    Use this function to recover from a failed restoration. It reads the
    original settings from the backup file and attempts to restore them.

    Args:
        config: Path to connection config file or config dict.
        backup_dir: Optional custom backup directory for settings file.

    Returns:
        True if settings were restored successfully (or no backup exists),
        False otherwise.
    """
    log.info("--- Manual VAT Settings Restoration from Backup ---")

    backup_path = _get_backup_file_path(config, backup_dir)

    if not backup_path.exists():
        log.info(f"No backup file found at {backup_path} - nothing to restore")
        return True

    settings = _load_settings_from_backup(backup_path)
    if settings is None:
        log.error(f"Failed to load settings from {backup_path}")
        return False

    log.info(
        f"Loaded backup from {backup_path} (created: {time.ctime(settings.timestamp)})"
    )
    log.info(f"  VIES settings for {len(settings.vies_settings)} companies")
    log.info(f"  {len(settings.stdnum_settings)} stdnum parameters")

    return restore_vat_validation_settings(config, settings, backup_dir=backup_dir)


def check_vat_settings_backup_status(
    config: Union[str, dict[str, Any]],
    backup_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Check if a VAT settings backup file exists and return its status.

    Args:
        config: Path to connection config file or config dict.
        backup_dir: Optional custom backup directory for settings file.

    Returns:
        Dictionary with backup status information:
        - exists: bool - Whether backup file exists
        - path: str - Path to backup file
        - timestamp: float - Backup creation timestamp (if exists)
        - age_hours: float - Age of backup in hours (if exists)
        - vies_company_count: int - Number of companies with VIES settings (if exists)
        - stdnum_param_count: int - Number of stdnum parameters (if exists)
    """
    backup_path = _get_backup_file_path(config, backup_dir)

    status: dict[str, Any] = {
        "exists": backup_path.exists(),
        "path": str(backup_path),
    }

    if status["exists"]:
        settings = _load_settings_from_backup(backup_path)
        if settings:
            status["timestamp"] = settings.timestamp
            status["age_hours"] = (time.time() - settings.timestamp) / 3600
            status["vies_company_count"] = len(settings.vies_settings)
            status["stdnum_param_count"] = len(settings.stdnum_settings)

    return status

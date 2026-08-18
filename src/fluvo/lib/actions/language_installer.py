"""This module contains the logic for installing languages in Odoo."""

import time
from typing import Any

from ...logging_config import log
from .. import conf_lib, odoo_lib


def _wait_for_languages_to_be_active(
    connection: Any, languages: list[str], timeout: int = 300
) -> bool:
    """Polls Odoo until the specified languages are active, with a timeout."""
    log.info(
        f"Waiting for languages to become active: {languages} (timeout: {timeout}s)"
    )
    lang_model = connection.get_model("res.lang")
    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            installed_langs_data = lang_model.search_read(
                [("code", "in", languages), ("active", "=", True)], ["code"]
            )
            active_langs = {lang["code"] for lang in installed_langs_data}

            if set(languages).issubset(active_langs):
                log.info("All requested languages are now active.")
                return True

            log.info(f"Still waiting... Active so far: {sorted(list(active_langs))}")
            time.sleep(5)  # Wait 5 seconds before polling again

        except Exception as e:
            log.error(f"An error occurred while checking language status: {e}")
            return False

    log.error("Timeout reached while waiting for languages to become active.")
    return False


def run_language_installation(config: str, languages: list[str]) -> bool:
    """Installs a list of languages into the Odoo database.

    Args:
        config: Path to the connection configuration file.
        languages: A list of language codes to install (e.g., ['de_DE', 'fr_FR']).

    Returns:
        True if all languages were installed successfully, False otherwise.
    """
    try:
        connection = conf_lib.get_connection_from_config(config_file=config)
        odoo_version = odoo_lib.get_odoo_version(connection)

        installer_model = connection.get_model("base.language.install")
        all_success = True

        for lang_code in languages:
            log.info(f"Preparing to install language: {lang_code}...")
            try:
                wizard_vals: dict[str, Any] = {}
                # base.language.install changed shape across versions: Odoo <= 15
                # uses a single 'lang' Selection; Odoo 16+ uses a 'lang_ids'
                # many2many (verified against a real Odoo 16 — the wizard has no
                # 'langs' field, so the old <17 'langs' branch failed on 16).
                if odoo_version < 16:
                    wizard_vals = {"lang": lang_code, "overwrite": False}
                else:
                    lang_model = connection.get_model("res.lang")
                    lang_ids = lang_model.search(
                        [("code", "=", lang_code)],
                        context={"active_test": False},
                    )
                    if not lang_ids:
                        log.error(f"Language code '{lang_code}' not found in Odoo.")
                        all_success = False
                        continue
                    wizard_vals = {
                        "lang_ids": [(6, 0, lang_ids)],
                        "overwrite": False,
                    }

                wizard_id = installer_model.create(wizard_vals)
                installer_model.lang_install([wizard_id])
                log.info(f"Successfully installed language '{lang_code}'.")

            except Exception as e:
                log.error(f"Failed to install language '{lang_code}': {e}")
                all_success = False

        return all_success
    except Exception as e:
        log.error(f"Could not connect to Odoo for language installation: {e}")
        return False

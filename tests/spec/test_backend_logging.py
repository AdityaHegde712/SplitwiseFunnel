import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.app.config import append_payment_mapping, load_config
from src.observability.logging import configure_logging


class BackendLoggingContractTests(unittest.TestCase):
    def test_logs_configuration_lifecycle_without_participant_or_card_values(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            config_path = temporary_path / "household.json"
            config_path.write_text(
                json.dumps(
                    {
                        "participant_ids": ["aditya_hegde"],
                        "payment_mappings": [],
                        "absences": [],
                        "rules": [],
                    }
                ),
                encoding="utf-8",
            )
            configure_logging(temporary_path / "logs")

            load_config(config_path)
            append_payment_mapping(config_path, "4592", "aditya_hegde")

            log_contents = (temporary_path / "logs" / "splitwise_funnel.log").read_text(
                encoding="utf-8"
            )
            self.assertIn('"event":"config_loaded"', log_contents)
            self.assertIn('"event":"payment_mapping_appended"', log_contents)
            self.assertNotIn("aditya_hegde", log_contents)
            self.assertNotIn("4592", log_contents)

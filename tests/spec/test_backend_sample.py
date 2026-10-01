import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.diagnostics.backend_sample import main


class BackendSampleContractTests(unittest.TestCase):
    @patch("src.diagnostics.backend_sample.fetch_receipt_emails", return_value=[])
    @patch("src.diagnostics.backend_sample.build_gmail_service")
    def test_records_no_matching_receipts_without_mutating_config(
        self,
        build_service: object,
        fetch_emails: object,
    ) -> None:
        del build_service, fetch_emails
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "household.json"
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

            exit_code = main(
                [
                    "--config", str(config_path),
                    "--output", str(root / "output"),
                    "--log-dir", str(root / "logs"),
                    "--start-on", "2026-09-11",
                    "--end-on", "2026-09-20",
                ]
            )

            manifest = json.loads((root / "output" / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 1)
            self.assertEqual(manifest["status"], "no_matching_receipts")
            self.assertFalse(manifest["raw_email_bodies_persisted"])
            self.assertFalse(manifest["manual_payer_supplied"])

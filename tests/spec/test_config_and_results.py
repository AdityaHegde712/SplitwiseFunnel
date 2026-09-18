import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.app.config import (
    ConfigError,
    append_participant_and_payment_mapping,
    append_payment_mapping,
    load_config,
    resolve_payer,
)
from src.app.results import build_result, render_markdown_summary, write_result_files
from src.domain.allocate import allocate_receipt


VALID_CONFIG = {
    "participant_ids": ["himanshu", "krishna", "nitish"],
    "payment_mappings": [
        {
            "last_four": "4821",
            "payer_id": "krishna",
        }
    ],
    "absences": [],
    "rules": [],
}


class ConfigAndResultContractTests(unittest.TestCase):
    def test_load_config_rejects_a_payment_mapping_to_an_unknown_participant(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "config.json"
            invalid_config = {
                **VALID_CONFIG,
                "payment_mappings": [
                    {
                        "last_four": "4821",
                        "payer_id": "unknown",
                    }
                ],
            }
            config_path.write_text(json.dumps(invalid_config), encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_resolve_payer_uses_household_wide_last_four_or_explicit_manual_payer(self) -> None:
        self.assertEqual(
            resolve_payer(
                payment_last_four="4821",
                explicit_payer_id=None,
                config=VALID_CONFIG,
            ),
            ("krishna", "card_last_four"),
        )
        self.assertEqual(
            resolve_payer(
                payment_last_four=None,
                explicit_payer_id="nitish",
                config=VALID_CONFIG,
            ),
            ("nitish", "manual"),
        )

    def test_appends_an_unknown_visa_mapping_to_the_private_config(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "household.json"
            config_path.write_text(json.dumps(VALID_CONFIG), encoding="utf-8")

            updated_config = append_payment_mapping(
                config_path,
                last_four="7192",
                payer_id="himanshu",
            )

            self.assertIn(
                {
                    "last_four": "7192",
                    "payer_id": "himanshu",
                },
                updated_config["payment_mappings"],
            )
            self.assertEqual(load_config(config_path), updated_config)

    def test_creates_a_participant_and_its_unknown_visa_mapping(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "household.json"
            config_path.write_text(json.dumps(VALID_CONFIG), encoding="utf-8")

            updated_config = append_participant_and_payment_mapping(
                config_path,
                last_four="9001",
                participant_id="aditya_hegde",
            )

            self.assertIn("aditya_hegde", updated_config["participant_ids"])
            self.assertEqual(
                resolve_payer("9001", None, updated_config),
                ("aditya_hegde", "card_last_four"),
            )

    def test_result_files_are_auditable_and_summary_totals_reconcile(self) -> None:
        receipt = {
            "receipt_id": "walmart-123",
            "retailer": "walmart_online",
            "purchase_date": "2026-09-17",
            "payment_last_four": "4821",
            "final_total_cents": 1001,
            "items": [{"description": "Rice", "amount_cents": 1000}],
        }
        allocation = allocate_receipt(receipt, VALID_CONFIG)
        result = build_result(
            receipt=receipt,
            payer_id="krishna",
            payer_resolution="card_last_four",
            allocation=allocation,
        )

        self.assertEqual(result["participant_totals_cents"], {"himanshu": 335, "krishna": 333, "nitish": 333})
        self.assertEqual(result["allocation"]["total_allocated_cents"], receipt["final_total_cents"])
        self.assertIn("$10.01", render_markdown_summary(result))
        self.assertIn("Krishna", render_markdown_summary(result))
        self.assertIn("## Item allocation", render_markdown_summary(result))
        self.assertIn("Rice — Himanshu $3.34, Krishna $3.33, Nitish $3.33", render_markdown_summary(result))
        self.assertIn("## Receipt-level residual", render_markdown_summary(result))

        with TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory)
            json_path, markdown_path = write_result_files(result, output_directory, "20260917_120000")

            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8")), result)
            self.assertIn("Per-person totals", markdown_path.read_text(encoding="utf-8"))

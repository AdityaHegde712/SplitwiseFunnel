import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.cli import (
    DEFAULT_VENDOR_IDS,
    RECEIPT_SIGNATURES,
    choose_unknown_payer,
    process_emails_with_unknown_payment_resolution,
    parse_arguments,
)
from src.gmail.client import ReceiptEmail


FIXTURES = Path(__file__).parent.parent / "fixtures"


class CommandLineContractTests(unittest.TestCase):
    def test_accepts_local_paths_dates_and_a_manual_payer(self) -> None:
        arguments = parse_arguments([
            "--config", "C:/private/household.json",
            "--output", "C:/private/results",
            "--start-on", "2026-09-01",
            "--end-on", "2026-09-17",
            "--vendors", "costco_same_day",
            "--manual-payer", "nitish",
        ])

        self.assertEqual(arguments.config, Path("C:/private/household.json"))
        self.assertEqual(arguments.output, Path("C:/private/results"))
        self.assertEqual(arguments.vendors, ["costco_same_day"])
        self.assertEqual(arguments.manual_payer, "nitish")

    def test_defaults_to_both_supported_vendors(self) -> None:
        arguments = parse_arguments([
            "--config", "C:/private/household.json",
            "--output", "C:/private/results",
            "--start-on", "2026-09-01",
            "--end-on", "2026-09-17",
        ])

        self.assertEqual(arguments.vendors, list(DEFAULT_VENDOR_IDS))

    def test_unknown_payment_prompt_can_select_or_create_a_participant(self) -> None:
        existing_answers = iter(["2"])
        messages: list[str] = []
        self.assertEqual(
            choose_unknown_payer(
                ["aditya_hegde", "krishna_mula"],
                input_func=lambda _: next(existing_answers),
                output_func=messages.append,
            ),
            ("krishna_mula", False),
        )

        new_answers = iter(["new: Kushagra Bainsla"])
        self.assertEqual(
            choose_unknown_payer(
                ["aditya_hegde"],
                input_func=lambda _: next(new_answers),
                output_func=lambda _: None,
            ),
            ("kushagra_bainsla", True),
        )

    def test_unknown_visa_selection_is_saved_then_the_receipt_continues(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "household.json"
            config_path.write_text(
                json.dumps(
                    {
                        "participant_ids": ["aditya_hegde", "krishna_mula"],
                        "payment_mappings": [],
                        "absences": [],
                        "rules": [],
                    }
                ),
                encoding="utf-8",
            )
            email = ReceiptEmail(
                message_id="one",
                sender="orders@instacart.com",
                subject="Your Instacart order receipt",
                html=(FIXTURES / "instacart_walmart_receipt.html")
                .read_text(encoding="utf-8")
                .replace("Your Visa 4821 was charged", "Your Visa 9001 was charged"),
            )
            selections = iter(["2"])

            results = process_emails_with_unknown_payment_resolution(
                [email],
                config_path,
                RECEIPT_SIGNATURES,
                input_func=lambda _: next(selections),
                output_func=lambda _: None,
            )

            saved_config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(results[0]["payer"]["participant_id"], "krishna_mula")
            self.assertEqual(
                saved_config["payment_mappings"],
                [{"last_four": "9001", "payer_id": "krishna_mula"}],
            )

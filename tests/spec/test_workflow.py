from pathlib import Path
import unittest

from src.app.workflow import UnknownPaymentMappingError, WorkflowError, process_receipt_emails
from src.gmail.client import ReceiptEmail


FIXTURES = Path(__file__).parent.parent / "fixtures"


class ReceiptWorkflowContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "participant_ids": ["himanshu", "krishna", "nitish"],
            "payment_mappings": [
                {"last_four": "4821", "payer_id": "krishna"},
                {"last_four": "7316", "payer_id": "himanshu"},
            ],
            "absences": [],
            "rules": [],
        }
        self.signatures = {
            "walmart_online": {
                "sender": "orders@instacart.com",
                "subject": "Your Instacart order receipt",
            },
            "costco_same_day": {
                "sender": "no-reply@costco.com",
                "subject": "Your Costco order receipt",
            },
        }

    def test_processes_allowlisted_receipts_to_reconciled_results(self) -> None:
        emails = [
            ReceiptEmail(
                message_id="one",
                sender="orders@instacart.com",
                subject="Your Instacart order receipt",
                html=(FIXTURES / "instacart_walmart_receipt.html").read_text(encoding="utf-8"),
            ),
            ReceiptEmail(
                message_id="two",
                sender="no-reply@costco.com",
                subject="Your Costco order receipt",
                html=(FIXTURES / "costco_receipt.html").read_text(encoding="utf-8"),
            ),
        ]

        results = process_receipt_emails(emails, self.config, self.signatures)

        self.assertEqual([result["receipt"]["retailer"] for result in results], [
            "walmart_online", "costco_same_day"
        ])
        self.assertEqual(results[0]["payer"], {"participant_id": "krishna", "resolution": "card_last_four"})
        self.assertEqual(results[1]["payer"], {"participant_id": "himanshu", "resolution": "card_last_four"})
        self.assertEqual(results[0]["allocation"]["total_allocated_cents"], 579)
        self.assertEqual(results[1]["allocation"]["total_allocated_cents"], 3681)

    def test_requires_manual_payer_when_payment_has_no_mapping_key(self) -> None:
        paypal_html = (FIXTURES / "instacart_walmart_receipt.html").read_text(encoding="utf-8")
        email = ReceiptEmail(
            message_id="one",
            sender="orders@instacart.com",
            subject="Your Instacart order receipt",
            html=paypal_html.replace("Your Visa 4821 was charged", "Your PayPal Card was charged"),
        )

        with self.assertRaises(WorkflowError):
            process_receipt_emails([email], self.config, self.signatures)

        result = process_receipt_emails(
            [email], self.config, self.signatures, manual_payer_id="nitish"
        )[0]
        self.assertEqual(result["payer"], {"participant_id": "nitish", "resolution": "manual"})

    def test_identifies_an_unknown_visa_mapping_without_exposing_email_content(self) -> None:
        html = (FIXTURES / "instacart_walmart_receipt.html").read_text(encoding="utf-8")
        email = ReceiptEmail(
            message_id="one",
            sender="orders@instacart.com",
            subject="Your Instacart order receipt",
            html=html.replace("Your Visa 4821 was charged", "Your Visa 9001 was charged"),
        )

        with self.assertRaises(UnknownPaymentMappingError) as context:
            process_receipt_emails([email], self.config, self.signatures)

        self.assertEqual(context.exception.retailer, "walmart_online")
        self.assertEqual(context.exception.last_four, "9001")

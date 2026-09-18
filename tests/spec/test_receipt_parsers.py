from pathlib import Path
import unittest

from src.parsers.receipts import ReceiptParseError, parse_receipt_email


FIXTURES = Path(__file__).parent.parent / "fixtures"


class ReceiptParserContractTests(unittest.TestCase):
    def test_parses_instacart_walmart_final_items_and_total(self) -> None:
        receipt = parse_receipt_email(
            "walmart_online", (FIXTURES / "instacart_walmart_receipt.html").read_text(encoding="utf-8")
        )

        self.assertEqual(receipt["receipt_id"], "W-100")
        self.assertEqual(receipt["purchase_date"], "2026-09-17")
        self.assertEqual(receipt["payment_method"], "visa")
        self.assertEqual(receipt["payment_last_four"], "4821")
        self.assertEqual(receipt["final_total_cents"], 579)
        self.assertEqual(receipt["items"], [
            {"description": "Organic Bananas", "quantity": 1, "amount_cents": 164},
            {"description": "Whole Milk", "quantity": 1, "amount_cents": 332},
        ])

    def test_parses_costco_final_items_and_total(self) -> None:
        receipt = parse_receipt_email(
            "costco_same_day", (FIXTURES / "costco_receipt.html").read_text(encoding="utf-8")
        )

        self.assertEqual(receipt["receipt_id"], "C-200")
        self.assertEqual(receipt["purchase_date"], "2026-09-13")
        self.assertEqual(receipt["payment_method"], "visa")
        self.assertEqual(receipt["payment_last_four"], "7316")
        self.assertEqual(receipt["final_total_cents"], 3681)
        self.assertEqual(receipt["items"][1], {"description": "Whole Milk Yogurt", "quantity": 3, "amount_cents": 2472})

    def test_marks_paypal_without_inventing_a_card_mapping_key(self) -> None:
        html = (FIXTURES / "instacart_walmart_receipt.html").read_text(encoding="utf-8")
        receipt = parse_receipt_email(
            "walmart_online",
            html.replace("Your Visa 4821 was charged", "Your PayPal Card was charged"),
        )

        self.assertEqual(receipt["payment_method"], "paypal")
        self.assertIsNone(receipt["payment_last_four"])

    def test_fails_when_a_receipt_has_no_final_total(self) -> None:
        with self.assertRaises(ReceiptParseError):
            parse_receipt_email("walmart_online", "<html><body>Order ID: # W-1</body></html>")

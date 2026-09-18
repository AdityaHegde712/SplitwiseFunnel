import unittest

from src.gmail.receipt_filters import build_receipt_query, matches_receipt_signature


SIGNATURES = {
    "walmart_online": {
        "sender": "orders@instacart.com",
        "subject": "Your Instacart order receipt",
    },
    "costco_same_day": {
        "sender": "no-reply@costco.com",
        "subject": "Your Costco order receipt",
    },
}


class ReceiptFilterContractTests(unittest.TestCase):
    def test_builds_a_narrow_query_for_selected_vendors_and_dates(self) -> None:
        query = build_receipt_query(
            vendor_ids=["walmart_online", "costco_same_day"],
            starts_on="2026-09-01",
            ends_on="2026-09-30",
            signatures=SIGNATURES,
        )

        self.assertEqual(
            query,
            'after:2026/09/01 before:2026/10/01 '
            '(from:orders@instacart.com subject:"Your Instacart order receipt" '
            'OR from:no-reply@costco.com subject:"Your Costco order receipt")',
        )

    def test_requires_exact_configured_sender_and_subject_before_parsing(self) -> None:
        self.assertTrue(
            matches_receipt_signature(
                sender="orders@instacart.com",
                subject="Your Instacart order receipt",
                signature=SIGNATURES["walmart_online"],
            )
        )
        self.assertFalse(
            matches_receipt_signature(
                sender="orders@instacart.com",
                subject="Your Instacart order receipt - altered",
                signature=SIGNATURES["walmart_online"],
            )
        )

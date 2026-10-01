import unittest

from src.gmail.receipt_filters import (
    build_receipt_query,
    build_vendor_subject_queries,
    matches_receipt_signature,
)
from src.cli import RECEIPT_SIGNATURES


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
            '{from:orders@instacart.com subject:"Your Instacart order receipt" '
            'OR from:no-reply@costco.com subject:"Your Costco order receipt"}',
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

    def test_allows_only_the_two_exact_instacart_subject_variants(self) -> None:
        signature = RECEIPT_SIGNATURES["walmart_online"]

        query = build_receipt_query(
            vendor_ids=["walmart_online"],
            starts_on="2026-09-11",
            ends_on="2026-09-20",
            signatures=RECEIPT_SIGNATURES,
        )

        self.assertTrue(
            matches_receipt_signature(
                "orders@instacart.com", "Instacart Order Receipt", signature
            )
        )
        self.assertFalse(
            matches_receipt_signature(
                "orders@instacart.com", "Instacart Order Receipt - forwarded", signature
            )
        )
        self.assertIn('subject:"Your Instacart order receipt"', query)
        self.assertIn('subject:"Instacart Order Receipt"', query)

    def test_builds_a_separate_exact_query_for_each_instacart_subject(self) -> None:
        queries = build_vendor_subject_queries(
            "walmart_online",
            "2026-09-11",
            "2026-09-20",
            RECEIPT_SIGNATURES,
        )

        self.assertEqual(len(queries), 2)
        self.assertTrue(all("from:orders@instacart.com" in query for query in queries))
        self.assertTrue(all(" OR " not in query for query in queries))
        self.assertIn('subject:"Your Instacart order receipt"', queries[0])
        self.assertIn('subject:"Instacart Order Receipt"', queries[1])

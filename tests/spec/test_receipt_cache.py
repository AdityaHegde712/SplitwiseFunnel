from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.app.receipt_cache import (
    clear_cached_ingestions,
    load_cached_ingestion,
    receipt_cache_path,
    save_ingestion,
)


class ReceiptCacheContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.cache_directory = Path(self.temporary_directory.name) / "receipt-cache"
        self.start_on = "2026-09-07"
        self.end_on = "2026-09-21"
        self.vendors = ["walmart_online", "costco_same_day"]
        self.records = [
            {
                "status": "parsed",
                "source": {
                    "message_id": "message-1",
                    "vendor_id": "walmart_online",
                    "email_sender": "orders@instacart.com",
                    "email_subject": "Your Instacart order receipt",
                },
                "receipt": {"receipt_id": "W-1", "retailer": "walmart_online"},
            }
        ]

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_saves_and_reloads_one_exact_local_ingestion_without_raw_email_bodies(self) -> None:
        cache_path = save_ingestion(
            self.cache_directory,
            self.start_on,
            self.end_on,
            self.vendors,
            self.records,
        )

        cached_records = load_cached_ingestion(
            self.cache_directory,
            self.start_on,
            self.end_on,
            list(reversed(self.vendors)),
        )

        self.assertTrue(cache_path.is_file())
        self.assertEqual(cached_records, self.records)
        self.assertNotIn("<html", cache_path.read_text(encoding="utf-8"))

    def test_does_not_reuse_a_cache_for_a_different_selection(self) -> None:
        save_ingestion(
            self.cache_directory,
            self.start_on,
            self.end_on,
            self.vendors,
            self.records,
        )

        cached_records = load_cached_ingestion(
            self.cache_directory,
            self.start_on,
            "2026-09-22",
            self.vendors,
        )

        self.assertIsNone(cached_records)

    def test_uses_a_stable_cache_path_for_vendor_order(self) -> None:
        first_path = receipt_cache_path(
            self.cache_directory,
            self.start_on,
            self.end_on,
            self.vendors,
        )
        second_path = receipt_cache_path(
            self.cache_directory,
            self.start_on,
            self.end_on,
            list(reversed(self.vendors)),
        )

        self.assertEqual(first_path, second_path)
    def test_clear_cached_ingestions_clears_files_and_resets_manual_receipts(self) -> None:
        save_ingestion(
            self.cache_directory,
            self.start_on,
            self.end_on,
            self.vendors,
            self.records,
        )
        manual_receipts_path = Path(self.temporary_directory.name) / "manual_receipts.json"
        manual_receipts_path.write_text('{"msg1": {"message_id": "msg1", "reason": "test"}}', encoding="utf-8")

        cleared = clear_cached_ingestions(self.cache_directory, manual_receipts_path=manual_receipts_path)

        self.assertEqual(cleared, 1)
        self.assertEqual(list(self.cache_directory.glob("*.json")), [])
        self.assertEqual(manual_receipts_path.read_text(encoding="utf-8").strip(), "{}")


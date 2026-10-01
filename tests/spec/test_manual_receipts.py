from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.app.manual_receipts import (
    ManualReceiptError,
    load_manual_receipts,
    save_manual_receipt,
)


class ManualReceiptsContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.quarantine_path = Path(self.temp_dir.name) / "manual_receipts.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_loads_empty_dict_when_file_does_not_exist(self) -> None:
        result = load_manual_receipts(self.quarantine_path)
        self.assertEqual(result, {})

    def test_saves_and_loads_manual_receipt_entry(self) -> None:
        entry = save_manual_receipt(
            self.quarantine_path,
            message_id="msg_001",
            reason="unsupported_receipt_format",
            metadata={"retailer": "walmart_online", "purchase_date": "2026-09-17"},
        )
        self.assertEqual(entry["message_id"], "msg_001")
        self.assertEqual(entry["reason"], "unsupported_receipt_format")
        self.assertEqual(entry["metadata"]["retailer"], "walmart_online")

        loaded = load_manual_receipts(self.quarantine_path)
        self.assertIn("msg_001", loaded)
        self.assertEqual(loaded["msg_001"]["message_id"], "msg_001")
        self.assertEqual(loaded["msg_001"]["reason"], "unsupported_receipt_format")
        self.assertEqual(loaded["msg_001"]["metadata"]["retailer"], "walmart_online")

    def test_overwrites_or_updates_existing_entry_by_message_id(self) -> None:
        save_manual_receipt(self.quarantine_path, message_id="msg_001", reason="initial")
        save_manual_receipt(self.quarantine_path, message_id="msg_001", reason="updated")

        loaded = load_manual_receipts(self.quarantine_path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded["msg_001"]["reason"], "updated")

    def test_rejects_empty_message_id_or_invalid_path(self) -> None:
        with self.assertRaises(ManualReceiptError):
            save_manual_receipt(self.quarantine_path, message_id="")

        with self.assertRaises(ManualReceiptError):
            save_manual_receipt("not_a_path", message_id="msg_001")  # type: ignore[arg-type]

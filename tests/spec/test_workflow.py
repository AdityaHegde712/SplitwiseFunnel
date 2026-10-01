from pathlib import Path
import unittest

from src.app.workflow import (
    ReceiptRefundLinkRequiredError,
    ReceiptReviewRequiredError,
    UnknownPaymentMappingError,
    WorkflowError,
    ingest_receipt_emails,
    process_cached_receipts,
    process_receipt_emails,
)

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
        self.assertEqual(context.exception.receipt_id, "W-100")
        self.assertEqual(context.exception.purchase_date, "2026-09-17")
        self.assertEqual(context.exception.amount_cents, 579)
        self.assertEqual(context.exception.email_sender, "orders@instacart.com")

    def test_identifies_an_unparseable_receipt_for_review_without_email_content(self) -> None:
        email = ReceiptEmail(
            message_id="one",
            sender="orders@instacart.com",
            subject="Your Instacart order receipt",
            html="<html><body>Unsupported receipt</body></html>",
        )

        with self.assertRaises(ReceiptReviewRequiredError) as context:
            process_receipt_emails([email], self.config, self.signatures)

        self.assertEqual(context.exception.retailer, "walmart_online")
        self.assertEqual(context.exception.reason_code, "unsupported_receipt_format")
        self.assertEqual(context.exception.email_sender, "orders@instacart.com")
        self.assertEqual(context.exception.email_subject, "Your Instacart order receipt")
        self.assertEqual(context.exception.message_id, "one")
        self.assertIsNotNone(context.exception.failure_detail)

    def test_processes_local_parsed_records_without_reparsing_email_bodies(self) -> None:
        email = ReceiptEmail(
            message_id="one",
            sender="orders@instacart.com",
            subject="Your Instacart order receipt",
            html=(FIXTURES / "instacart_walmart_receipt.html").read_text(encoding="utf-8"),
        )
        records = ingest_receipt_emails([email], self.signatures)

        results = process_cached_receipts(records, self.config)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["receipt"]["receipt_id"], "W-100")
        self.assertEqual(results[0]["payer"]["resolution"], "card_last_four")

    def test_unlinked_refund_in_cached_run_raises_link_required_error_with_candidates(
        self,
    ) -> None:
        records = [
            {
                "status": "parsed",
                "source": {
                    "message_id": "orig-1",
                    "vendor_id": "walmart_online",
                    "email_sender": "orders@instacart.com",
                    "email_subject": "Your Instacart order receipt",
                },
                "receipt": {
                    "receipt_id": "W-100",
                    "retailer": "walmart_online",
                    "purchase_date": "2026-09-17",
                    "final_total_cents": 4500,
                    "items": [{"description": "Item", "amount_cents": 4500, "quantity": 1}],
                    "payment_method": "visa",
                    "payment_last_four": "4821",
                },
            },
            {
                "status": "refund_pending_link",
                "source": {
                    "message_id": "ref-1",
                    "vendor_id": "walmart_online",
                    "email_sender": "orders@instacart.com",
                    "email_subject": "Your Instacart order receipt",
                },
                "refund": {
                    "refund_id": "ref-1",
                    "retailer": "walmart_online",
                    "refund_date": "2026-09-18",
                    "refund_amount_cents": 1000,
                    "source": {
                        "message_id": "ref-1",
                        "vendor_id": "walmart_online",
                        "email_sender": "orders@instacart.com",
                        "email_subject": "Your Instacart order receipt",
                    },
                },
            },
        ]

        with self.assertRaises(ReceiptRefundLinkRequiredError) as context:
            process_cached_receipts(records, self.config)

        self.assertEqual(context.exception.refund["refund_id"], "ref-1")
        self.assertEqual(context.exception.refund["refund_amount_cents"], 1000)
        self.assertEqual(len(context.exception.candidates), 1)
        self.assertEqual(context.exception.candidates[0]["receipt_id"], "W-100")
        self.assertEqual(context.exception.candidates[0]["final_total_cents"], 4500)

    def test_linked_refund_applies_negative_adjustment_equally_to_present_participants(
        self,
    ) -> None:
        records = [
            {
                "status": "parsed",
                "source": {
                    "message_id": "orig-1",
                    "vendor_id": "walmart_online",
                    "email_sender": "orders@instacart.com",
                    "email_subject": "Your Instacart order receipt",
                },
                "receipt": {
                    "receipt_id": "W-100",
                    "retailer": "walmart_online",
                    "purchase_date": "2026-09-17",
                    "final_total_cents": 4500,
                    "items": [{"description": "Item", "amount_cents": 4500, "quantity": 1}],
                    "payment_method": "visa",
                    "payment_last_four": "4821",
                },
            },
            {
                "status": "refund_pending_link",
                "source": {
                    "message_id": "ref-1",
                    "vendor_id": "walmart_online",
                    "email_sender": "orders@instacart.com",
                    "email_subject": "Your Instacart order receipt",
                },
                "refund": {
                    "refund_id": "ref-1",
                    "retailer": "walmart_online",
                    "refund_date": "2026-09-18",
                    "refund_amount_cents": 1000,
                    "source": {
                        "message_id": "ref-1",
                        "vendor_id": "walmart_online",
                        "email_sender": "orders@instacart.com",
                        "email_subject": "Your Instacart order receipt",
                    },
                },
            },
        ]
        # Nitish is absent on original purchase date, so refund should divide between himanshu and krishna
        config_with_absence = {
            **self.config,
            "absences": [
                {"participant_id": "nitish", "starts_on": "2026-09-15", "ends_on": "2026-09-20"}
            ],
        }
        refund_links = {
            "ref-1": {
                "refund_id": "ref-1",
                "original_receipt_id": "W-100",
                "refund_amount_cents": 1000,
            }
        }

        results = process_cached_receipts(
            records, config_with_absence, refund_links=refund_links
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["original_total_cents"], 4500)
        self.assertEqual(results[0]["refund_adjustment_cents"], -1000)
        self.assertEqual(results[0]["net_total_cents"], 3500)
        # Original 4500 was split between himanshu (2250) and krishna (2250)
        # Refund -1000 was split between himanshu (-500) and krishna (-500)
        # Net for each is 1750
        self.assertEqual(results[0]["participant_totals_cents"]["himanshu"], 1750)
        self.assertEqual(results[0]["participant_totals_cents"]["krishna"], 1750)
        self.assertEqual(results[0]["participant_totals_cents"].get("nitish", 0), 0)
        self.assertEqual(sum(results[0]["participant_totals_cents"].values()), 3500)

    def test_quarantined_receipt_bypasses_review_and_unknown_payment_error(self) -> None:
        records = [
            {
                "status": "parsed",
                "source": {
                    "message_id": "valid-1",
                    "vendor_id": "walmart_online",
                    "email_sender": "orders@instacart.com",
                    "email_subject": "Your Instacart order receipt",
                },
                "receipt": {
                    "receipt_id": "W-100",
                    "retailer": "walmart_online",
                    "purchase_date": "2026-09-17",
                    "final_total_cents": 579,
                    "items": [{"description": "Item", "amount_cents": 579, "quantity": 1}],
                    "payment_method": "visa",
                    "payment_last_four": "4821",
                },
            },
            {
                "status": "review_required",
                "source": {
                    "message_id": "bad-layout-msg",
                    "vendor_id": "walmart_online",
                    "email_sender": "orders@instacart.com",
                    "email_subject": "Your Instacart order receipt",
                },
                "reason_code": "unsupported_receipt_format",
                "failure_detail": "Failed parsing rows",
            },
            {
                "status": "parsed",
                "source": {
                    "message_id": "unknown-card-msg",
                    "vendor_id": "walmart_online",
                    "email_sender": "orders@instacart.com",
                    "email_subject": "Your Instacart order receipt",
                },
                "receipt": {
                    "receipt_id": "W-999",
                    "retailer": "walmart_online",
                    "purchase_date": "2026-09-18",
                    "final_total_cents": 1200,
                    "items": [{"description": "Item", "amount_cents": 1200, "quantity": 1}],
                    "payment_method": "visa",
                    "payment_last_four": "9999",
                },
            },
        ]

        manual_receipts = {
            "bad-layout-msg": {
                "message_id": "bad-layout-msg",
                "reason": "unsupported_receipt_format",
            },
            "unknown-card-msg": {
                "message_id": "unknown-card-msg",
                "reason": "unknown_payment_mapping",
            },
        }

        results = process_cached_receipts(
            records, self.config, manual_receipts=manual_receipts
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["receipt"]["receipt_id"], "W-100")
        self.assertTrue(hasattr(results, "manual_receipts"))
        quarantined = results.manual_receipts
        self.assertEqual(len(quarantined), 2)
        quarantined_ids = {m["message_id"] for m in quarantined}
        self.assertEqual(quarantined_ids, {"bad-layout-msg", "unknown-card-msg"})

    def test_auto_quarantine_routes_unsupported_receipt_without_blocking_batch(self) -> None:
        records = [
            {
                "status": "parsed",
                "source": {
                    "message_id": "valid-1",
                    "vendor_id": "walmart_online",
                    "email_sender": "orders@instacart.com",
                    "email_subject": "Your Instacart order receipt",
                },
                "receipt": {
                    "receipt_id": "W-100",
                    "retailer": "walmart_online",
                    "purchase_date": "2026-09-17",
                    "final_total_cents": 579,
                    "items": [{"description": "Item", "amount_cents": 579, "quantity": 1}],
                    "payment_method": "visa",
                    "payment_last_four": "4821",
                },
            },
            {
                "status": "review_required",
                "source": {
                    "message_id": "bad-layout-msg",
                    "vendor_id": "walmart_online",
                    "email_sender": "orders@instacart.com",
                    "email_subject": "Your Instacart order receipt",
                },
                "reason_code": "unsupported_receipt_format",
                "failure_detail": "Failed parsing rows",
            },
        ]

        results = process_cached_receipts(
            records, self.config, auto_quarantine=True
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["receipt"]["receipt_id"], "W-100")
        self.assertEqual(len(results.manual_receipts), 1)
        self.assertEqual(results.manual_receipts[0]["message_id"], "bad-layout-msg")

    def test_auto_quarantine_does_not_quarantine_unknown_payment_mapping(self) -> None:
        records = [
            {
                "status": "parsed",
                "source": {
                    "message_id": "unknown-card-msg",
                    "vendor_id": "walmart_online",
                    "email_sender": "orders@instacart.com",
                    "email_subject": "Your Instacart order receipt",
                },
                "receipt": {
                    "receipt_id": "W-999",
                    "retailer": "walmart_online",
                    "purchase_date": "2026-09-18",
                    "final_total_cents": 1200,
                    "items": [{"description": "Item", "amount_cents": 1200, "quantity": 1}],
                    "payment_method": "visa",
                    "payment_last_four": "9999",
                },
            },
        ]

        with self.assertRaises(UnknownPaymentMappingError):
            process_cached_receipts(records, self.config, auto_quarantine=True)

        manual_receipts = {
            "unknown-card-msg": {
                "message_id": "unknown-card-msg",
                "reason": "unknown_payment_mapping",
            }
        }
        bypassed_results = process_cached_receipts(
            records, self.config, manual_receipts=manual_receipts, auto_quarantine=True
        )
        self.assertEqual(len(bypassed_results), 0)
        self.assertEqual(len(bypassed_results.manual_receipts), 1)
        self.assertEqual(bypassed_results.manual_receipts[0]["message_id"], "unknown-card-msg")



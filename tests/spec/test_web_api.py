import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.web.app import create_app
from src.web.server import main as web_server_main
from src.gmail.auth import GmailAuthError
from src.gmail.client import GmailRateLimitError, ReceiptEmail
from src.app.workflow import (
    ReceiptRefundLinkRequiredError,
    ReceiptReviewRequiredError,
    UnknownPaymentMappingError,
)



class LocalWebApiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        temporary_path = Path(self.temporary_directory.name)
        self.config_path = temporary_path / "household.json"
        self.log_directory = temporary_path / "logs"
        self.config_path.write_text(
            json.dumps(
                {
                    "participant_ids": ["aditya_hegde"],
                    "payment_mappings": [{"last_four": "4592", "payer_id": "aditya_hegde"}],
                    "absences": [],
                    "rules": [],
                }
            ),
            encoding="utf-8",
        )
        self.client = TestClient(create_app(self.config_path, self.log_directory))

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_serves_health_ui_and_a_household_view(self) -> None:
        health_response = self.client.get("/api/v1/health")
        page_response = self.client.get("/")
        household_response = self.client.get("/api/v1/household")

        self.assertEqual(health_response.status_code, 200)
        self.assertEqual(health_response.json(), {"status": "ok"})
        self.assertIn("Run receipts", page_response.text)
        self.assertIn("resolution-details-list", page_response.text)
        self.assertEqual(household_response.status_code, 200)
        self.assertEqual(household_response.json()["participant_ids"], ["aditya_hegde"])
        self.assertEqual(
            household_response.headers["x-correlation-id"], household_response.json()["correlation_id"]
        )

    def test_serves_an_aggregate_summary_binding_in_the_review_ui(self) -> None:
        script_response = self.client.get("/static/app.js")

        self.assertEqual(script_response.status_code, 200)
        self.assertIn("Run total · all receipts", script_response.text)
        self.assertIn("aggregate_summary", script_response.text)

    def test_serves_refund_resolution_ui_components(self) -> None:
        page_response = self.client.get("/")
        script_response = self.client.get("/static/app.js")
        styles_response = self.client.get("/static/styles.css")

        self.assertEqual(page_response.status_code, 200)
        self.assertIn('id="refund-resolution"', page_response.text)
        self.assertIn('id="refund-candidate"', page_response.text)
        self.assertIn('id="refund-resolution-form"', page_response.text)
        self.assertIn("Link refund and rerun", page_response.text)

        self.assertEqual(script_response.status_code, 200)
        self.assertIn("refund_receipt_link_required", script_response.text)
        self.assertIn("/api/v1/refund-links", script_response.text)
        self.assertIn("refund-resolution", script_response.text)
        self.assertIn("refund-candidate", script_response.text)

        self.assertEqual(styles_response.status_code, 200)
        self.assertIn(".warning-card", styles_response.text)

    def test_updates_household_setup_through_validated_api_calls(self) -> None:
        participant_response = self.client.post(
            "/api/v1/household/participants", json={"participant_id": "krishna_mula"}
        )
        mapping_response = self.client.post(
            "/api/v1/household/payment-mappings",
            json={"last_four": "1468", "payer_id": "krishna_mula"},
        )

        self.assertEqual(participant_response.status_code, 201)
        self.assertEqual(mapping_response.status_code, 201)
        persisted_config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertIn("krishna_mula", persisted_config["participant_ids"])
        self.assertIn(
            {"last_four": "1468", "payer_id": "krishna_mula"},
            persisted_config["payment_mappings"],
        )

    def test_logs_each_api_exchange_with_a_correlation_id(self) -> None:
        response = self.client.get("/api/v1/household", headers={"X-Correlation-ID": "web-42"})

        self.assertEqual(response.status_code, 200)
        log_contents = (self.log_directory / "splitwise_funnel.log").read_text(encoding="utf-8")
        self.assertIn('"event":"http_request_completed"', log_contents)
        self.assertIn('"correlation_id":"web-42"', log_contents)

    def test_replaces_raw_household_configuration_after_validation(self) -> None:
        response = self.client.put(
            "/api/v1/household",
            json={
                "participant_ids": ["aditya_hegde", "nitish_kumar"],
                "payment_mappings": [
                    {"last_four": "4592", "payer_id": "aditya_hegde"}
                ],
                "absences": [],
                "rules": [],
            },
        )

        self.assertEqual(response.status_code, 200)
        persisted_config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted_config["participant_ids"], ["aditya_hegde", "nitish_kumar"])

    @patch("src.web.app.process_cached_receipts")
    @patch("src.web.app.load_cached_ingestion")
    def test_runs_receipts_and_lists_persisted_transaction_history(
        self,
        load_cached: object,
        process_records: object,
    ) -> None:
        load_cached.return_value = [
            {
                "status": "parsed",
                "source": {
                    "message_id": "message-1",
                    "vendor_id": "walmart_online",
                    "email_sender": "orders@instacart.com",
                    "email_subject": "Your Instacart order receipt",
                },
                "receipt": {},
            }
        ]
        process_records.return_value = [
            {
                "receipt": {
                    "receipt_id": "order-1",
                    "retailer": "walmart_online",
                    "purchase_date": "2026-09-17",
                    "final_total_cents": 1200,
                },
                "payer": {"participant_id": "aditya_hegde", "resolution": "card_last_four"},
                "allocation": {
                    "items": [],
                    "residual_shares_cents": {"aditya_hegde": 1200},
                    "total_allocated_cents": 1200,
                },
                "participant_totals_cents": {"aditya_hegde": 1200},
            }
        ]

        run_response = self.client.post(
            "/api/v1/runs",
            json={
                "start_on": "2026-09-01",
                "end_on": "2026-09-18",
                "vendors": ["walmart_online"],
            },
        )
        history_response = self.client.get("/api/v1/runs")

        self.assertEqual(run_response.status_code, 201)
        self.assertEqual(run_response.json()["receipt_count"], 1)
        self.assertEqual(run_response.json()["receipt_source"], "local_cache")
        self.assertEqual(history_response.status_code, 200)
        self.assertEqual(history_response.json()["runs"][0]["receipt_id"], "order-1")

    @patch("src.web.app.process_cached_receipts")
    @patch("src.web.app.load_cached_ingestion")
    def test_returns_an_aggregate_total_for_the_completed_receipt_run(
        self,
        load_cached: object,
        process_records: object,
    ) -> None:
        load_cached.return_value = [{}]
        process_records.return_value = [
            {
                "receipt": {"receipt_id": "order-1", "retailer": "walmart_online", "purchase_date": "2026-09-17", "final_total_cents": 1200},
                "participant_totals_cents": {"aditya_hegde": 600, "nitish_kumar": 600},
                "payer": {"participant_id": "aditya_hegde"},
                "allocation": {"items": [], "residual_shares_cents": {}, "total_allocated_cents": 1200},
            },
            {
                "receipt": {"receipt_id": "order-2", "retailer": "walmart_online", "purchase_date": "2026-09-18", "final_total_cents": 500},
                "participant_totals_cents": {"aditya_hegde": 250, "nitish_kumar": 250},
                "payer": {"participant_id": "nitish_kumar"},
                "allocation": {"items": [], "residual_shares_cents": {}, "total_allocated_cents": 500},
            },
        ]

        response = self.client.post(
            "/api/v1/runs",
            json={
                "start_on": "2026-09-01",
                "end_on": "2026-09-18",
                "vendors": ["walmart_online"],
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["aggregate_totals_cents"]["aditya_hegde"], 850)
        self.assertEqual(response.json()["aggregate_totals_cents"]["nitish_kumar"], 850)
        self.assertIn("# Run total", response.json()["aggregate_summary"])

    @patch("src.web.app.process_cached_receipts")
    @patch("src.web.app.load_cached_ingestion")
    def test_reports_unknown_payment_mapping_for_local_resolution(
        self,
        load_cached: object,
        process_records: object,
    ) -> None:
        load_cached.return_value = [{}]
        process_records.side_effect = UnknownPaymentMappingError(
            "walmart_online",
            "9999",
            receipt_id="message-2",
            purchase_date="2026-09-17",
            payment_method="visa",
            amount_cents=1500,
            email_sender="orders@instacart.com",
            email_subject="Your Instacart order receipt",
        )

        response = self.client.post(
            "/api/v1/runs",
            json={
                "start_on": "2026-09-01",
                "end_on": "2026-09-18",
                "vendors": ["walmart_online"],
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["last_four"], "9999")
        self.assertEqual(response.json()["purchase_date"], "2026-09-17")
        self.assertEqual(response.json()["amount_cents"], 1500)
        self.assertEqual(response.json()["email_sender"], "orders@instacart.com")

    @patch("src.web.app.process_cached_receipts")
    @patch("src.web.app.load_cached_ingestion")
    def test_reports_a_separate_receipt_that_needs_review(
        self,
        load_cached: object,
        process_records: object,
    ) -> None:
        load_cached.return_value = [{}]
        process_records.side_effect = ReceiptReviewRequiredError(
            retailer="walmart_online",
            reason_code="unsupported_receipt_format",
            email_sender="orders@instacart.com",
            email_subject="Your Instacart order receipt",
        )

        response = self.client.post(
            "/api/v1/runs",
            json={
                "start_on": "2026-09-01",
                "end_on": "2026-09-18",
                "vendors": ["walmart_online"],
                "manual_payer_id": "aditya_hegde",
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["reason_code"], "unsupported_receipt_format")
        self.assertEqual(response.json()["retailer"], "walmart_online")
        self.assertEqual(response.json()["email_sender"], "orders@instacart.com")

    @patch("src.web.app.process_cached_receipts")
    @patch("src.web.app.save_ingestion")
    @patch("src.web.app.ingest_receipt_emails")
    @patch("src.web.app.fetch_receipt_emails")
    @patch("src.web.app.build_gmail_service")
    @patch("src.web.app.load_cached_ingestion")
    def test_fetches_gmail_once_then_reuses_the_matching_local_ingestion(
        self,
        load_cached: object,
        build_service: object,
        fetch_emails: object,
        ingest_emails: object,
        save_cached: object,
        process_records: object,
    ) -> None:
        cached_records = [{"status": "parsed", "source": {}, "receipt": {}}]
        load_cached.side_effect = [None, cached_records]
        fetch_emails.return_value = [
            ReceiptEmail("message-4", "orders@instacart.com", "Your Instacart order receipt", "<html />")
        ]
        ingest_emails.return_value = cached_records
        process_records.return_value = []

        payload = {
            "start_on": "2026-09-01",
            "end_on": "2026-09-18",
            "vendors": ["walmart_online"],
        }
        first_response = self.client.post("/api/v1/runs", json=payload)
        second_response = self.client.post("/api/v1/runs", json=payload)

        self.assertEqual(first_response.status_code, 201)
        self.assertEqual(second_response.status_code, 201)
        self.assertEqual(first_response.json()["receipt_source"], "gmail")
        self.assertEqual(second_response.json()["receipt_source"], "local_cache")
        self.assertEqual(fetch_emails.call_count, 1)
        self.assertEqual(ingest_emails.call_count, 1)
        self.assertEqual(save_cached.call_count, 1)
        self.assertEqual(build_service.call_count, 1)

    @patch("src.web.app.fetch_receipt_emails")
    @patch("src.web.app.build_gmail_service")
    @patch("src.web.app.load_cached_ingestion")
    def test_reports_gmail_rate_limiting_without_attempting_processing(
        self,
        load_cached: object,
        build_service: object,
        fetch_emails: object,
    ) -> None:
        del build_service
        load_cached.return_value = None
        fetch_emails.side_effect = GmailRateLimitError("rate limited")

        response = self.client.post(
            "/api/v1/runs",
            json={
                "start_on": "2026-09-01",
                "end_on": "2026-09-18",
                "vendors": ["walmart_online"],
            },
        )

        self.assertEqual(response.status_code, 429)
        self.assertIn("no local cache was created", response.json()["detail"])

    @patch("src.web.app.build_gmail_service")
    @patch("src.web.app.load_cached_ingestion")
    def test_reports_gmail_authorization_failure_without_fetching_receipts(
        self,
        load_cached: object,
        build_service: object,
    ) -> None:
        load_cached.return_value = None
        build_service.side_effect = GmailAuthError(
            "The saved Gmail authorization token could not be refreshed."
        )

        response = self.client.post(
            "/api/v1/runs",
            json={
                "start_on": "2026-09-01",
                "end_on": "2026-09-18",
                "vendors": ["walmart_online"],
            },
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["reason_code"], "gmail_authorization_failed")
        self.assertIn("token.json", response.json()["detail"])

    @patch("src.web.server.uvicorn.run")
    @patch("src.web.server._default_log_directory")
    def test_server_is_hard_bound_to_loopback(
        self,
        log_directory: object,
        run_server: object,
    ) -> None:
        log_directory.return_value = self.log_directory
        exit_code = web_server_main(["--config", str(self.config_path), "--port", "8766"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(run_server.call_args.kwargs["host"], "127.0.0.1")
        self.assertEqual(run_server.call_args.kwargs["port"], 8766)

    @patch("src.web.app.process_cached_receipts")
    @patch("src.web.app.load_cached_ingestion")
    def test_reports_unlinked_refund_requiring_receipt_selection(
        self,
        load_cached: object,
        process_records: object,
    ) -> None:
        load_cached.return_value = [{}]
        process_records.side_effect = ReceiptRefundLinkRequiredError(
            refund={
                "refund_id": "ref-1",
                "retailer": "walmart_online",
                "refund_date": "2026-09-18",
                "refund_amount_cents": 1000,
            },
            candidates=[
                {"receipt_id": "W-100", "purchase_date": "2026-09-17", "final_total_cents": 4500}
            ],
            retailer="walmart_online",
        )

        response = self.client.post(
            "/api/v1/runs",
            json={
                "start_on": "2026-09-01",
                "end_on": "2026-09-18",
                "vendors": ["walmart_online"],
            },
        )

        self.assertEqual(response.status_code, 409)
        data = response.json()
        self.assertEqual(data["reason_code"], "refund_receipt_link_required")
        self.assertEqual(data["refund"]["refund_id"], "ref-1")
        self.assertEqual(len(data["candidates"]), 1)
        self.assertEqual(data["candidates"][0]["receipt_id"], "W-100")

    @patch("src.web.app.process_cached_receipts")
    @patch("src.web.app.load_cached_ingestion")
    def test_saves_refund_link_and_reruns_receipts(
        self,
        load_cached: object,
        process_records: object,
    ) -> None:
        load_cached.return_value = [{}]
        process_records.return_value = [
            {
                "receipt": {
                    "receipt_id": "W-100",
                    "retailer": "walmart_online",
                    "purchase_date": "2026-09-17",
                    "final_total_cents": 4500,
                },
                "payer": {"participant_id": "aditya_hegde", "resolution": "card_last_four"},
                "allocation": {"items": [], "residual_shares_cents": {}, "total_allocated_cents": 4500},
                "original_total_cents": 4500,
                "refund_adjustment_cents": -1000,
                "net_total_cents": 3500,
                "participant_totals_cents": {"aditya_hegde": 3500},
            }
        ]

        response = self.client.post(
            "/api/v1/refund-links",
            json={
                "refund_id": "ref-1",
                "original_receipt_id": "W-100",
                "refund_amount_cents": 1000,
                "refund_date": "2026-09-18",
                "start_on": "2026-09-01",
                "end_on": "2026-09-18",
                "vendors": ["walmart_online"],
            },
        )

        self.assertIn(response.status_code, (200, 201))
        data = response.json()
        self.assertEqual(data["receipt_count"], 1)
        self.assertEqual(data["aggregate_total_cents"], 3500)

    @patch("src.web.app.process_cached_receipts")
    @patch("src.web.app.load_cached_ingestion")
    def test_saves_manual_receipt_and_reruns(
        self,
        load_cached: object,
        process_records: object,
    ) -> None:
        load_cached.return_value = [{"status": "parsed", "source": {"message_id": "m1"}, "receipt": {}}]
        mock_result = {
            "receipt": {
                "receipt_id": "W-100",
                "retailer": "walmart_online",
                "purchase_date": "2026-09-17",
                "final_total_cents": 2500,
            },
            "payer": {"participant_id": "aditya_hegde", "resolution": "card_last_four"},
            "allocation": {"items": [], "residual_shares_cents": {}, "total_allocated_cents": 2500},
            "original_total_cents": 2500,
            "refund_adjustment_cents": 0,
            "net_total_cents": 2500,
            "participant_totals_cents": {"aditya_hegde": 2500},
        }
        process_records.return_value = [mock_result]

        response = self.client.post(
            "/api/v1/manual-receipts",
            json={
                "message_id": "msg-manual-1",
                "reason": "unsupported_receipt_format",
                "metadata": {"retailer": "walmart_online"},
                "start_on": "2026-09-01",
                "end_on": "2026-09-18",
                "vendors": ["walmart_online"],
            },
        )

        self.assertIn(response.status_code, (200, 201))
        data = response.json()
        self.assertIn("automated_count", data)
        self.assertIn("manual_count", data)
        self.assertIn("total_count", data)
        self.assertIn("manual_receipts", data)

    def test_serves_manual_entry_ui_components(self) -> None:
        page_response = self.client.get("/")
        script_response = self.client.get("/static/app.js")

        self.assertEqual(page_response.status_code, 200)
        self.assertIn("Mark for manual entry", page_response.text)

        self.assertEqual(script_response.status_code, 200)
        self.assertIn("/api/v1/manual-receipts", script_response.text)
        self.assertIn("automated_count", script_response.text)
        self.assertIn("Clear cached receipts", page_response.text)
        self.assertIn("/api/v1/cache/clear", script_response.text)

    def test_clear_cache_removes_json_files_and_returns_cleared_count(self) -> None:
        cache_dir = self.config_path.parent / "receipt_ingestions"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "receipt_ingestion_123.json").write_text("{}", encoding="utf-8")
        (cache_dir / "receipt_ingestion_456.json").write_text("{}", encoding="utf-8")
        manual_receipts_file = self.config_path.parent / "manual_receipts.json"
        manual_receipts_file.write_text('{"msg1": {"message_id": "msg1", "reason": "test"}}', encoding="utf-8")

        response = self.client.post("/api/v1/cache/clear")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cleared_count"], 2)
        self.assertEqual(list(cache_dir.glob("*.json")), [])
        self.assertEqual(manual_receipts_file.read_text(encoding="utf-8").strip(), "{}")

    @patch("src.web.app.process_cached_receipts")
    @patch("src.web.app.load_cached_ingestion")
    def test_runs_endpoint_returns_payer_aggregates(
        self,
        load_cached: object,
        process_records: object,
    ) -> None:
        load_cached.return_value = [{"status": "parsed"}]
        mock_results = [
            {
                "receipt": {
                    "receipt_id": "W-100",
                    "retailer": "walmart_online",
                    "purchase_date": "2026-09-17",
                    "final_total_cents": 1000,
                },
                "payer": {"participant_id": "aditya_hegde", "resolution": "card_last_four"},
                "allocation": {"items": [], "residual_shares_cents": {}, "total_allocated_cents": 1000},
                "participant_totals_cents": {"aditya_hegde": 1000},
                "net_total_cents": 1000,
            }
        ]
        process_records.return_value = mock_results

        response = self.client.post(
            "/api/v1/runs",
            json={
                "start_on": "2026-09-01",
                "end_on": "2026-09-18",
                "vendors": ["walmart_online"],
            },
        )

        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertIn("payer_aggregates", data)
        self.assertIn("aditya_hegde", data["payer_aggregates"])
        self.assertEqual(data["payer_aggregates"]["aditya_hegde"]["total_cents"], 1000)




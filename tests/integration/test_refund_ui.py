"""Integration test for the refund resolution UI flow."""

import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import threading
import time
import unittest
import urllib.request

from playwright.sync_api import sync_playwright
import uvicorn

from src.app.receipt_cache import save_ingestion
from src.web.app import create_app


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class RefundUiIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.temp_path = Path(self.temporary_directory.name)
        self.config_path = self.temp_path / "household.json"
        self.log_directory = self.temp_path / "logs"
        self.ingestion_directory = self.temp_path / "receipt_ingestions"

        self.config_path.write_text(
            json.dumps(
                {
                    "participant_ids": ["aditya_hegde", "nitish_kumar"],
                    "payment_mappings": [{"last_four": "4592", "payer_id": "aditya_hegde"}],
                    "absences": [],
                    "rules": [],
                }
            ),
            encoding="utf-8",
        )

        self.start_on = "2026-09-01"
        self.end_on = "2026-09-20"
        self.vendors = ["walmart_online", "costco_same_day"]

        # Cache fixture: 1 original receipt and 1 unlinked refund
        self.cached_records = [
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
                    "items": [
                        {"description": "Organic Honeycrisp Apples", "amount_cents": 4500, "quantity": 1}
                    ],
                    "payment_method": "visa",
                    "payment_last_four": "4592",
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
        save_ingestion(
            self.ingestion_directory,
            self.start_on,
            self.end_on,
            self.vendors,
            self.cached_records,
        )

        self.port = _find_free_port()
        self.app = create_app(self.config_path, self.log_directory)
        server_config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=self.port,
            log_level="error",
        )
        self.server = uvicorn.Server(server_config)
        self.server_thread = threading.Thread(target=self.server.run, daemon=True)
        self.server_thread.start()

        health_url = f"http://127.0.0.1:{self.port}/api/v1/health"
        for _ in range(60):
            try:
                with urllib.request.urlopen(health_url, timeout=0.5) as response:
                    if response.status == 200:
                        break
            except Exception:
                time.sleep(0.05)
        else:
            raise RuntimeError(f"FastAPI test server failed to start on port {self.port}")

    def tearDown(self) -> None:
        self.server.should_exit = True
        self.server_thread.join(timeout=3)
        self.temporary_directory.cleanup()

    def test_refund_resolution_flow(self) -> None:
        screenshots_dir = Path("data/screenshots")
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        modal_screenshot_path = screenshots_dir / "refund_resolution_modal.png"
        summary_screenshot_path = screenshots_dir / "refund_resolved_summary.png"

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(f"http://127.0.0.1:{self.port}/")

            # Set the date range matching our cached ingestion
            page.fill("#start-on", self.start_on)
            page.fill("#end-on", self.end_on)

            # Click "Fetch and review receipts"
            page.click('#run-form button[type="submit"]')

            # Assert #refund-resolution is displayed
            refund_card = page.locator("#refund-resolution")
            refund_card.wait_for(state="visible", timeout=6000)

            # Verify refund details are displayed
            details_text = page.locator("#refund-details-list").inner_text()
            self.assertIn("ref-1", details_text)
            self.assertIn("2026-09-18", details_text)
            self.assertIn("$10.00", details_text)
            self.assertIn("Walmart Online", details_text)

            # Verify candidate options
            candidate_select = page.locator("#refund-candidate")
            self.assertTrue(candidate_select.is_visible())
            option_texts = candidate_select.locator("option").all_inner_texts()
            self.assertTrue(any("W-100" in text and "$45.00" in text for text in option_texts))

            # Take screenshot of the pending refund card
            page.screenshot(path=str(modal_screenshot_path), full_page=True)
            self.assertTrue(modal_screenshot_path.is_file())

            # Select the candidate receipt and click "Link refund and rerun"
            candidate_select.select_option("W-100")
            page.click('#refund-resolution-form button[type="submit"]')

            # Assert run completes successfully and review state is displayed
            review_state = page.locator("#review-state")
            review_state.wait_for(state="visible", timeout=6000)

            # Assert refund-resolution card is hidden
            self.assertFalse(refund_card.is_visible())

            # Assert reconciled aggregate run total and per-person breakdown
            review_description = page.locator("#review-description").inner_text()
            self.assertIn("$35.00", review_description)

            summaries_content = page.locator("#summaries").inner_text()
            self.assertIn("Run total · all receipts", summaries_content)
            self.assertIn("$35.00", summaries_content)
            self.assertIn("Original total: $45.00", summaries_content)
            self.assertIn("Refund adjustment: -$10.00", summaries_content)
            self.assertIn("Net total: $35.00", summaries_content)
            self.assertIn("Refund adjusted", summaries_content)

            # Take screenshot of the completed reconciled review
            page.screenshot(path=str(summary_screenshot_path), full_page=True)
            self.assertTrue(summary_screenshot_path.is_file())

            browser.close()

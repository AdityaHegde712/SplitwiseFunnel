"""Integration test for Discover payment resolution flow in Playwright."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import threading
import time
import unittest
import urllib.request

from playwright.sync_api import sync_playwright
import uvicorn

from src.web.app import create_app


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class DiscoverPaymentResolutionIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.household_path = Path("data/household.json")
        self.manual_receipts_path = Path("data/manual_receipts.json")
        self.log_directory = Path("data/logs")

        # Ensure 5139 is not yet in data/household.json for test precondition
        if self.household_path.is_file():
            household_data = json.loads(self.household_path.read_text(encoding="utf-8"))
            mappings = household_data.get("payment_mappings", [])
            filtered_mappings = [m for m in mappings if m.get("last_four") != "5139"]
            if len(mappings) != len(filtered_mappings):
                household_data["payment_mappings"] = filtered_mappings
                self.household_path.write_text(json.dumps(household_data, indent=2) + "\n", encoding="utf-8")

        # Ensure manual_receipts is reset
        self.manual_receipts_path.write_text("{}\n", encoding="utf-8")

        self.port = 8766
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", self.port))
        except OSError:
            self.port = _find_free_port()

        self.app = create_app(
            config_path=self.household_path,
            log_directory=self.log_directory,
        )
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

    def test_discover_payment_resolution(self) -> None:
        screenshots_dir = Path("data/screenshots")
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        resolution_screenshot = screenshots_dir / "discover_mapping_resolution.png"
        resolved_screenshot = screenshots_dir / "discover_mapping_resolved.png"

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(f"http://127.0.0.1:{self.port}/")

            # Enter From 2026-04-01 Through 2026-09-27
            page.fill("#start-on", "2026-04-01")
            page.fill("#end-on", "2026-09-27")

            # Click "Fetch and review receipts"
            page.click('#run-form button[type="submit"]')

            # Verify #mapping-resolution is displayed
            mapping_card = page.locator("#mapping-resolution")
            mapping_card.wait_for(state="visible", timeout=10000)

            # Verify description and details
            desc_text = page.locator("#mapping-description").inner_text()
            self.assertIn("Discover ending in 5139 is not mapped", desc_text)

            details_text = page.locator("#resolution-details-list").inner_text()
            self.assertIn("2026-06-12", details_text)
            self.assertIn("Walmart Online", details_text)
            self.assertIn("$65.67", details_text)

            # Take screenshot: data/screenshots/discover_mapping_resolution.png
            page.screenshot(path=str(resolution_screenshot), full_page=True)
            self.assertTrue(resolution_screenshot.is_file())

            # Select a payer (e.g. Aditya Hegde) in #unknown-payer
            page.select_option("#unknown-payer", value="aditya_hegde")

            # Click "Save and rerun"
            page.click('#mapping-resolution-form button[type="submit"]')

            # Assert that data/household.json now contains the mapping {"last_four": "5139", "payer_id": "aditya_hegde"}
            mapping_found = False
            for _ in range(50):
                time.sleep(0.1)
                household_data = json.loads(self.household_path.read_text(encoding="utf-8"))
                for mapping in household_data.get("payment_mappings", []):
                    if mapping.get("last_four") == "5139" and mapping.get("payer_id") == "aditya_hegde":
                        mapping_found = True
                        break
                if mapping_found:
                    break

            self.assertTrue(mapping_found, "Mapping for 5139 -> aditya_hegde not found in data/household.json")

            # Wait a moment for page rendering after submission
            page.wait_for_timeout(1000)

            # Capture screenshot after submission: data/screenshots/discover_mapping_resolved.png
            page.screenshot(path=str(resolved_screenshot), full_page=True)
            self.assertTrue(resolved_screenshot.is_file())

            browser.close()

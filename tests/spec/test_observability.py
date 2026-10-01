from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.observability.logging import configure_logging, log_event


class ObservabilityContractTests(unittest.TestCase):
    def test_writes_verbose_structured_events_without_sensitive_payloads(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            logger = configure_logging(Path(temporary_directory))

            log_event(
                logger,
                "receipt_fetch_started",
                correlation_id="run-123",
                vendor_id="walmart_online",
                raw_email_body="PRIVATE EMAIL CONTENT",
                oauth_token="PRIVATE TOKEN",
                participant_id="aditya_hegde",
                payment_last_four="4592",
                receipt_count=2,
            )

            log_contents = (Path(temporary_directory) / "splitwise_funnel.log").read_text(
                encoding="utf-8"
            )
            self.assertIn('"event":"receipt_fetch_started"', log_contents)
            self.assertIn('"correlation_id":"run-123"', log_contents)
            self.assertIn('"receipt_count":2', log_contents)
            self.assertNotIn("PRIVATE EMAIL CONTENT", log_contents)
            self.assertNotIn("PRIVATE TOKEN", log_contents)
            self.assertNotIn("aditya_hegde", log_contents)
            self.assertNotIn("4592", log_contents)
            self.assertIn('"raw_email_body":"[REDACTED]"', log_contents)
            self.assertIn('"oauth_token":"[REDACTED]"', log_contents)
            self.assertIn('"participant_id":"[REDACTED]"', log_contents)
            self.assertIn('"payment_last_four":"[REDACTED]"', log_contents)

    def test_detaches_a_log_handler_when_its_directory_is_removed(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            logger = configure_logging(Path(temporary_directory))

        log_event(logger, "post_cleanup_event", correlation_id="run-456")
        self.assertFalse(logger.handlers)

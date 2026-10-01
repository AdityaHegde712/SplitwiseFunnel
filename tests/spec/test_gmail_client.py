import base64
import unittest
from unittest.mock import MagicMock, call, patch

from googleapiclient.errors import HttpError

from src.gmail.client import (
    GmailRateLimitError,
    _execute_gmail_request,
    fetch_receipt_emails,
)


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


def _encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


class _FakeRequest:
    def __init__(self, response: dict[str, object]) -> None:
        self._response = response

    def execute(self) -> dict[str, object]:
        return self._response


class _FakeMessages:
    def __init__(self, messages: dict[str, dict[str, object]]) -> None:
        self._messages = messages
        self.list_queries: list[str] = []
        self.get_ids: list[str] = []

    def list(self, *, userId: str, q: str, pageToken: str | None = None) -> _FakeRequest:
        self.list_queries.append(q)
        return _FakeRequest({"messages": [{"id": message_id} for message_id in self._messages]})

    def get(self, *, userId: str, id: str, format: str) -> _FakeRequest:
        self.get_ids.append(id)
        return _FakeRequest(self._messages[id])


class _FakeUsers:
    def __init__(self, messages: dict[str, dict[str, object]]) -> None:
        self._messages = _FakeMessages(messages)

    def messages(self) -> _FakeMessages:
        return self._messages


class _FakeService:
    def __init__(self, messages: dict[str, dict[str, object]]) -> None:
        self._users = _FakeUsers(messages)

    def users(self) -> _FakeUsers:
        return self._users


def _message(sender: str, subject: str, html: str) -> dict[str, object]:
    return {
        "id": "message-1",
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
            ],
            "mimeType": "multipart/alternative",
            "parts": [{"mimeType": "text/html", "body": {"data": _encoded(html)}}],
        },
    }


class GmailClientContractTests(unittest.TestCase):
    def test_fetches_only_messages_that_pass_exact_sender_and_subject_validation(self) -> None:
        service = _FakeService(
            {
                "message-1": _message(
                    "Instacart <orders@instacart.com>",
                    "Your Instacart order receipt",
                    "<p>Receipt body</p>",
                ),
                "message-2": _message(
                    "attacker@example.com",
                    "Your Instacart order receipt",
                    "<p>Never parse this body</p>",
                ),
            }
        )

        messages = fetch_receipt_emails(
            service=service,
            vendor_ids=["walmart_online"],
            starts_on="2026-09-01",
            ends_on="2026-09-30",
            signatures=SIGNATURES,
        )

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].message_id, "message-1")
        self.assertEqual(messages[0].sender, "orders@instacart.com")
        self.assertEqual(messages[0].html, "<p>Receipt body</p>")
        self.assertIn("from:orders@instacart.com", service.users().messages().list_queries[0])

    def test_rejects_a_matched_message_without_a_supported_body_part(self) -> None:
        message = _message(
            "orders@instacart.com",
            "Your Instacart order receipt",
            "<p>Ignored</p>",
        )
        message["payload"] = {
            "headers": message["payload"]["headers"],
            "mimeType": "application/pdf",
            "body": {"data": _encoded("not a parsed receipt")},
        }
        service = _FakeService({"message-1": message})

        with self.assertRaisesRegex(ValueError, "supported text body"):
            fetch_receipt_emails(
                service=service,
                vendor_ids=["walmart_online"],
                starts_on="2026-09-01",
                ends_on="2026-09-30",
                signatures=SIGNATURES,
            )

    def test_uses_one_narrow_query_per_selected_vendor(self) -> None:
        service = _FakeService(
            {
                "message-1": _message(
                    "orders@instacart.com",
                    "Your Instacart order receipt",
                    "<p>Walmart receipt</p>",
                ),
                "message-2": _message(
                    "no-reply@costco.com",
                    "Your Costco order receipt",
                    "<p>Costco receipt</p>",
                ),
            }
        )

        messages = fetch_receipt_emails(
            service=service,
            vendor_ids=["walmart_online", "costco_same_day"],
            starts_on="2026-09-01",
            ends_on="2026-09-30",
            signatures=SIGNATURES,
        )

        queries = service.users().messages().list_queries
        self.assertEqual(len(messages), 2)
        self.assertEqual(len(queries), 2)
        self.assertIn("from:orders@instacart.com", queries[0])
        self.assertNotIn("no-reply@costco.com", queries[0])
        self.assertIn("from:no-reply@costco.com", queries[1])
        self.assertNotIn("orders@instacart.com", queries[1])

    @patch("time.sleep")
    def test_retries_on_rate_limit_and_succeeds(self, mock_sleep: MagicMock) -> None:
        mock_request = MagicMock()
        rate_limit_error = HttpError(resp=MagicMock(status=429), content=b"Rate limit")
        mock_request.execute.side_effect = [rate_limit_error, {"messages": []}]

        response = _execute_gmail_request(mock_request)

        self.assertEqual(response, {"messages": []})
        self.assertEqual(mock_request.execute.call_count, 2)
        mock_sleep.assert_called_once_with(2.0)

    @patch("time.sleep")
    def test_retries_on_rate_limit_up_to_max_and_raises_rate_limit_error(
        self, mock_sleep: MagicMock
    ) -> None:
        mock_request = MagicMock()
        rate_limit_error = HttpError(resp=MagicMock(status=429), content=b"Rate limit")
        mock_request.execute.side_effect = [
            rate_limit_error,
            rate_limit_error,
            rate_limit_error,
        ]

        with self.assertRaises(GmailRateLimitError):
            _execute_gmail_request(mock_request)

        self.assertEqual(mock_request.execute.call_count, 3)
        self.assertEqual(mock_sleep.call_args_list, [call(2.0), call(5.0)])

    @patch("time.sleep")
    def test_paces_message_fetches_with_sleep(self, mock_sleep: MagicMock) -> None:
        service = _FakeService(
            {
                "message-1": _message(
                    "orders@instacart.com",
                    "Your Instacart order receipt",
                    "<p>Walmart receipt</p>",
                ),
            }
        )

        fetch_receipt_emails(
            service=service,
            vendor_ids=["walmart_online"],
            starts_on="2026-09-01",
            ends_on="2026-09-30",
            signatures=SIGNATURES,
        )

        mock_sleep.assert_any_call(0.05)

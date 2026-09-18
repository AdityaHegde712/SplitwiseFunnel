import base64
import unittest

from src.gmail.client import fetch_receipt_emails


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

"""Read allowlisted receipt messages through an injected Gmail service."""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email.utils import parseaddr
import logging
import time
from typing import Any

from googleapiclient.errors import HttpError

from src.gmail.receipt_filters import build_vendor_subject_queries, matches_receipt_signature
from src.observability.logging import log_event


LOGGER = logging.getLogger("splitwise_funnel.gmail.client")


class GmailRateLimitError(RuntimeError):
    """Raised when Gmail rejects a bounded local ingestion for quota reasons."""


@dataclass(frozen=True)
class ReceiptEmail:
    """A validated receipt email whose supported text body was decoded."""

    message_id: str
    sender: str
    subject: str
    html: str


def fetch_receipt_emails(
    service: Any,
    vendor_ids: Sequence[str],
    starts_on: str,
    ends_on: str,
    signatures: Mapping[str, Mapping[str, str]],
) -> list[ReceiptEmail]:
    """Fetch receipt emails with one narrow Gmail query per selected vendor."""
    log_event(LOGGER, "gmail_receipt_fetch_started", vendor_count=len(vendor_ids))
    messages_resource: Any = service.users().messages()
    receipt_emails: list[ReceiptEmail] = []
    page_count: int = 0
    seen_message_ids: set[str] = set()

    for vendor_id in vendor_ids:
        signature = signatures[vendor_id]
        queries = build_vendor_subject_queries(vendor_id, starts_on, ends_on, signatures)
        for query in queries:
            page_token: str | None = None
            while True:
                response = _execute_gmail_request(
                    messages_resource.list(userId="me", q=query, pageToken=page_token)
                )
                page_count += 1
                message_refs: object = response.get("messages", [])
                if not isinstance(message_refs, list):
                    raise ValueError("Gmail message listing returned an invalid messages collection.")

                for message_ref in message_refs:
                    message_id = _message_id(message_ref)
                    if message_id in seen_message_ids:
                        continue
                    time.sleep(0.05)
                    message = _execute_gmail_request(
                        messages_resource.get(userId="me", id=message_id, format="full")
                    )
                    sender, subject = _message_headers(message)
                    if not matches_receipt_signature(sender, subject, signature):
                        log_event(LOGGER, "gmail_receipt_skipped", status="signature_mismatch")
                        continue

                    seen_message_ids.add(message_id)
                    body = _extract_supported_body(message)
                    receipt_emails.append(
                        ReceiptEmail(
                            message_id=message_id,
                            sender=sender,
                            subject=subject,
                            html=body,
                        )
                    )

                next_page_token: object = response.get("nextPageToken")
                if next_page_token is None:
                    break
                if not isinstance(next_page_token, str) or not next_page_token:
                    raise ValueError("Gmail message listing returned an invalid next page token.")
                page_token = next_page_token

    log_event(
        LOGGER,
        "gmail_receipt_fetch_completed",
        status="completed",
        page_count=page_count,
        receipt_count=len(receipt_emails),
    )
    return receipt_emails


def _execute_gmail_request(
    request: Any,
    *,
    max_retries: int = 3,
    retry_delays: Sequence[float] = (2.0, 5.0),
) -> Mapping[str, Any]:
    """Translate a Gmail quota response without recording provider payloads in logs."""
    for attempt in range(max_retries):
        try:
            response: object = request.execute()
            if not isinstance(response, Mapping):
                raise ValueError("Gmail request returned an invalid response object.")
            return response
        except HttpError as error:
            if not _is_gmail_rate_limit(error):
                raise
            if attempt < max_retries - 1:
                delay = retry_delays[attempt] if attempt < len(retry_delays) else retry_delays[-1]
                time.sleep(delay)
                continue
            log_event(LOGGER, "gmail_receipt_fetch_rate_limited", status="rate_limited")
            raise GmailRateLimitError(
                "Gmail temporarily rate limited receipt ingestion. Wait before retrying; "
                "no local cache was created for this selection."
            ) from error
    raise RuntimeError("Unreachable")


def _is_gmail_rate_limit(error: Exception) -> bool:
    response = getattr(error, "resp", None)
    status = getattr(response, "status", None)
    return status == 429 or "rateLimitExceeded" in str(error)


def _message_id(message_ref: object) -> str:
    if not isinstance(message_ref, Mapping):
        raise ValueError("Gmail message listing contains an invalid message reference.")
    message_id: object = message_ref.get("id")
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("Gmail message listing contains a message without an id.")
    return message_id


def _message_headers(message: Mapping[str, Any]) -> tuple[str, str]:
    payload: Mapping[str, Any] = _payload(message)
    headers: object = payload.get("headers", [])
    if not isinstance(headers, list):
        return "", ""

    raw_sender: str = ""
    subject: str = ""
    for header in headers:
        if not isinstance(header, Mapping):
            continue
        name: object = header.get("name")
        value: object = header.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        if name.casefold() == "from":
            raw_sender = value
        elif name.casefold() == "subject":
            subject = value

    _, sender = parseaddr(raw_sender)
    return sender, subject


def _extract_supported_body(message: Mapping[str, Any]) -> str:
    payload: Mapping[str, Any] = _payload(message)
    html_bodies: list[str] = []
    plain_bodies: list[str] = []
    _collect_supported_bodies(payload, html_bodies, plain_bodies)
    if html_bodies:
        return html_bodies[0]
    if plain_bodies:
        return plain_bodies[0]
    message_id: object = message.get("id")
    identifier: str = message_id if isinstance(message_id, str) else "unknown"
    raise ValueError(f"Receipt message {identifier!r} has no supported text body.")


def _payload(message: Mapping[str, Any]) -> Mapping[str, Any]:
    payload: object = message.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("Gmail message is missing a valid payload.")
    return payload


def _collect_supported_bodies(
    part: Mapping[str, Any], html_bodies: list[str], plain_bodies: list[str]
) -> None:
    mime_type: object = part.get("mimeType")
    if mime_type == "text/html":
        decoded_body: str | None = _decode_body(part)
        if decoded_body is not None:
            html_bodies.append(decoded_body)
    elif mime_type == "text/plain":
        decoded_body = _decode_body(part)
        if decoded_body is not None:
            plain_bodies.append(decoded_body)

    child_parts: object = part.get("parts", [])
    if not isinstance(child_parts, list):
        return
    for child_part in child_parts:
        if isinstance(child_part, Mapping):
            _collect_supported_bodies(child_part, html_bodies, plain_bodies)


def _decode_body(part: Mapping[str, Any]) -> str | None:
    body: object = part.get("body")
    if not isinstance(body, Mapping):
        return None
    encoded_body: object = body.get("data")
    if not isinstance(encoded_body, str) or not encoded_body:
        return None

    padded_body: str = encoded_body + "=" * (-len(encoded_body) % 4)
    try:
        return base64.urlsafe_b64decode(padded_body).decode("utf-8")
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("Receipt message contains an invalid base64url text body.") from error

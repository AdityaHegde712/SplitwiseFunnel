"""Read allowlisted receipt messages through an injected Gmail service."""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Any

from src.gmail.receipt_filters import build_receipt_query, matches_receipt_signature


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
    """Fetch receipt emails that exactly match the selected sender signatures."""
    query: str = build_receipt_query(vendor_ids, starts_on, ends_on, signatures)
    selected_signatures: tuple[Mapping[str, str], ...] = tuple(
        signatures[vendor_id] for vendor_id in vendor_ids
    )
    messages_resource: Any = service.users().messages()
    receipt_emails: list[ReceiptEmail] = []
    page_token: str | None = None

    while True:
        response: Mapping[str, Any] = messages_resource.list(
            userId="me", q=query, pageToken=page_token
        ).execute()
        message_refs: object = response.get("messages", [])
        if not isinstance(message_refs, list):
            raise ValueError("Gmail message listing returned an invalid messages collection.")

        for message_ref in message_refs:
            message_id: str = _message_id(message_ref)
            message: Mapping[str, Any] = messages_resource.get(
                userId="me", id=message_id, format="full"
            ).execute()
            sender, subject = _message_headers(message)
            matching_signature: Mapping[str, str] | None = next(
                (
                    signature
                    for signature in selected_signatures
                    if matches_receipt_signature(sender, subject, signature)
                ),
                None,
            )
            if matching_signature is None:
                continue

            body: str = _extract_supported_body(message)
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
            return receipt_emails
        if not isinstance(next_page_token, str) or not next_page_token:
            raise ValueError("Gmail message listing returned an invalid next page token.")
        page_token = next_page_token


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

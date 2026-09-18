"""Strict Gmail query and receipt-signature helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any


def build_receipt_query(
    vendor_ids: Sequence[str],
    starts_on: str,
    ends_on: str,
    signatures: Mapping[str, Mapping[str, str]],
) -> str:
    """Build an allowlisted Gmail query with inclusive date bounds."""
    start_date: date = _parse_iso_date(starts_on)
    end_date: date = _parse_iso_date(ends_on)
    if end_date < start_date:
        raise ValueError("ends_on must not precede starts_on.")

    alternatives: list[str] = []
    for vendor_id in vendor_ids:
        try:
            signature: Mapping[str, str] = signatures[vendor_id]
            sender: str = signature["sender"]
            subject: str = signature["subject"]
        except KeyError as error:
            raise ValueError(f"Unknown or incomplete receipt signature: {vendor_id!r}.") from error
        alternatives.append(f'from:{sender} subject:"{subject}"')

    if not alternatives:
        raise ValueError("At least one vendor must be selected.")

    exclusive_end_date: date = end_date + timedelta(days=1)
    return (
        f"after:{start_date:%Y/%m/%d} before:{exclusive_end_date:%Y/%m/%d} "
        f"({' OR '.join(alternatives)})"
    )


def matches_receipt_signature(
    sender: str,
    subject: str,
    signature: Mapping[str, Any],
) -> bool:
    """Return whether message headers exactly match an allowlisted receipt."""
    return sender == signature.get("sender") and subject == signature.get("subject")


def _parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Dates must use ISO YYYY-MM-DD format.") from error

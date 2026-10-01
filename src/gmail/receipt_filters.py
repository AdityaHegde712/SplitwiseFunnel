"""Strict Gmail query and receipt-signature helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any


def build_receipt_query(
    vendor_ids: Sequence[str],
    starts_on: str,
    ends_on: str,
    signatures: Mapping[str, Mapping[str, object]],
) -> str:
    """Build an allowlisted Gmail query with inclusive date bounds."""
    start_date: date = _parse_iso_date(starts_on)
    end_date: date = _parse_iso_date(ends_on)
    if end_date < start_date:
        raise ValueError("ends_on must not precede starts_on.")

    alternatives: list[str] = []
    for vendor_id in vendor_ids:
        try:
            signature: Mapping[str, object] = signatures[vendor_id]
            sender: str = signature["sender"]
            subjects = _subjects_for_signature(signature)
        except KeyError as error:
            raise ValueError(f"Unknown or incomplete receipt signature: {vendor_id!r}.") from error
        alternatives.extend(f'from:{sender} subject:"{subject}"' for subject in subjects)

    if not alternatives:
        raise ValueError("At least one vendor must be selected.")

    exclusive_end_date: date = end_date + timedelta(days=1)
    return (
        f"after:{start_date:%Y/%m/%d} before:{exclusive_end_date:%Y/%m/%d} "
        f"{{{' OR '.join(alternatives)}}}"
    )


def build_vendor_subject_queries(
    vendor_id: str,
    starts_on: str,
    ends_on: str,
    signatures: Mapping[str, Mapping[str, object]],
) -> tuple[str, ...]:
    """Build one exact Gmail query per allowed subject for a selected vendor."""
    start_date = _parse_iso_date(starts_on)
    end_date = _parse_iso_date(ends_on)
    if end_date < start_date:
        raise ValueError("ends_on must not precede starts_on.")
    try:
        signature = signatures[vendor_id]
        sender = signature["sender"]
        subjects = _subjects_for_signature(signature)
    except KeyError as error:
        raise ValueError(f"Unknown or incomplete receipt signature: {vendor_id!r}.") from error
    if not isinstance(sender, str) or not sender:
        raise ValueError(f"Unknown or incomplete receipt signature: {vendor_id!r}.")
    exclusive_end_date = end_date + timedelta(days=1)
    return tuple(
        f'after:{start_date:%Y/%m/%d} before:{exclusive_end_date:%Y/%m/%d} '
        f'from:{sender} subject:"{subject}"'
        for subject in subjects
    )


def matches_receipt_signature(
    sender: str,
    subject: str,
    signature: Mapping[str, Any],
) -> bool:
    """Return whether message headers exactly match an allowlisted receipt."""
    return sender == signature.get("sender") and subject in _subjects_for_signature(signature)


def _subjects_for_signature(signature: Mapping[str, Any]) -> tuple[str, ...]:
    """Normalize one or more exact subject strings from an allowlist entry."""
    subject_value: object = signature.get("subject")
    if isinstance(subject_value, str) and subject_value:
        return (subject_value,)
    if isinstance(subject_value, tuple) and subject_value and all(
        isinstance(subject, str) and subject for subject in subject_value
    ):
        return subject_value
    raise ValueError("Receipt signature requires one or more non-empty subjects.")


def _parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Dates must use ISO YYYY-MM-DD format.") from error

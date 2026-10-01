"""Persistence and retrieval for owner-linked receipt refund adjustments."""

from __future__ import annotations

from collections.abc import Mapping
import json
import logging
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from src.observability.logging import log_event


LOGGER = logging.getLogger("splitwise_funnel.app.refund_links")
DEFAULT_REFUND_LINKS_PATH = Path("data/refund_links.json")


class RefundLinkError(ValueError):
    """Raised when a refund link cannot be safely stored or retrieved."""


def load_refund_links(path: Path) -> dict[str, dict[str, Any]]:
    """Load existing refund-to-receipt mappings from a JSON sidecar file."""
    if not isinstance(path, Path):
        raise RefundLinkError("Refund links path must be a pathlib.Path.")
    if not path.is_file():
        return {}

    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        log_event(LOGGER, "refund_links_load_failed", error_type=type(error).__name__)
        raise RefundLinkError("Failed to decode refund links JSON.") from error

    if not isinstance(content, dict):
        raise RefundLinkError("Refund links payload must be a JSON object.")

    validated: dict[str, dict[str, Any]] = {}
    for refund_id, link_entry in content.items():
        if not isinstance(refund_id, str) or not refund_id.strip():
            raise RefundLinkError("Refund ID must be a non-empty string.")
        if not isinstance(link_entry, Mapping):
            raise RefundLinkError(f"Refund link entry for {refund_id!r} must be an object.")
        validated[refund_id] = _validate_link_entry(refund_id, link_entry)

    log_event(LOGGER, "refund_links_loaded", link_count=len(validated))
    return validated


def save_refund_link(
    path: Path,
    refund_id: str,
    original_receipt_id: str,
    refund_amount_cents: int | None = None,
    refund_date: str | None = None,
) -> dict[str, Any]:
    """Persist a link between a refund message and an original receipt idempotently."""
    if not isinstance(path, Path):
        raise RefundLinkError("Refund links path must be a pathlib.Path.")
    if not isinstance(refund_id, str) or not refund_id.strip():
        raise RefundLinkError("refund_id must be a non-empty string.")
    if not isinstance(original_receipt_id, str) or not original_receipt_id.strip():
        raise RefundLinkError("original_receipt_id must be a non-empty string.")

    cleaned_refund_id = refund_id.strip()
    entry: dict[str, Any] = {
        "refund_id": cleaned_refund_id,
        "original_receipt_id": original_receipt_id.strip(),
    }
    if refund_amount_cents is not None:
        if isinstance(refund_amount_cents, bool) or not isinstance(refund_amount_cents, int):
            raise RefundLinkError("refund_amount_cents must be an integer.")
        if refund_amount_cents <= 0:
            raise RefundLinkError("refund_amount_cents must be positive.")
        entry["refund_amount_cents"] = refund_amount_cents

    if refund_date is not None:
        if not isinstance(refund_date, str) or not refund_date.strip():
            raise RefundLinkError("refund_date must be a non-empty string if provided.")
        entry["refund_date"] = refund_date.strip()

    links = load_refund_links(path)
    links[cleaned_refund_id] = entry
    _write_atomic(path, links)
    log_event(
        LOGGER,
        "refund_link_saved",
        refund_id=cleaned_refund_id,
        original_receipt_id=original_receipt_id.strip(),
    )
    return entry


def _validate_link_entry(refund_id: str, entry: Mapping[str, Any]) -> dict[str, Any]:
    original_receipt_id = entry.get("original_receipt_id")
    if not isinstance(original_receipt_id, str) or not original_receipt_id.strip():
        raise RefundLinkError(f"Missing valid original_receipt_id for refund {refund_id!r}.")

    validated_entry: dict[str, Any] = {
        "refund_id": refund_id,
        "original_receipt_id": original_receipt_id.strip(),
    }
    if "refund_amount_cents" in entry:
        amount = entry["refund_amount_cents"]
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise RefundLinkError(f"Invalid refund_amount_cents for refund {refund_id!r}.")
        validated_entry["refund_amount_cents"] = amount

    if "refund_date" in entry:
        ref_date = entry["refund_date"]
        if isinstance(ref_date, str) and ref_date.strip():
            validated_entry["refund_date"] = ref_date.strip()

    return validated_entry


def _write_atomic(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
        newline="\n",
    ) as temp_file:
        temp_file.write(serialized)
        temp_path = Path(temp_file.name)
    temp_path.replace(path)

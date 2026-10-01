"""Persistence and retrieval for manually processed receipt quarantines."""

from __future__ import annotations

from collections.abc import Mapping
import json
import logging
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from src.observability.logging import log_event


LOGGER = logging.getLogger("splitwise_funnel.app.manual_receipts")
DEFAULT_MANUAL_RECEIPTS_PATH = Path("data/manual_receipts.json")


class ManualReceiptError(ValueError):
    """Raised when a manual receipt quarantine entry cannot be safely stored or retrieved."""


def load_manual_receipts(path: Path) -> dict[str, dict[str, Any]]:
    """Load quarantined manual receipts from a JSON sidecar file."""
    if not isinstance(path, Path):
        raise ManualReceiptError("Manual receipts path must be a pathlib.Path.")
    if not path.is_file():
        return {}

    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        log_event(LOGGER, "manual_receipts_load_failed", error_type=type(error).__name__)
        raise ManualReceiptError("Failed to decode manual receipts JSON.") from error

    if not isinstance(content, dict):
        raise ManualReceiptError("Manual receipts payload must be a JSON object.")

    validated: dict[str, dict[str, Any]] = {}
    for message_id, entry in content.items():
        if not isinstance(message_id, str) or not message_id.strip():
            raise ManualReceiptError("message_id must be a non-empty string.")
        if not isinstance(entry, Mapping):
            raise ManualReceiptError(f"Manual receipt entry for {message_id!r} must be an object.")
        validated[message_id] = _validate_manual_entry(message_id, entry)

    log_event(LOGGER, "manual_receipts_loaded", count=len(validated))
    return validated


def save_manual_receipt(
    path: Path,
    message_id: str,
    reason: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a manual process quarantine entry idempotently."""
    if not isinstance(path, Path):
        raise ManualReceiptError("Manual receipts path must be a pathlib.Path.")
    if not isinstance(message_id, str) or not message_id.strip():
        raise ManualReceiptError("message_id must be a non-empty string.")

    cleaned_message_id = message_id.strip()
    entry: dict[str, Any] = {
        "message_id": cleaned_message_id,
        "reason": reason.strip() if isinstance(reason, str) else "",
    }
    if metadata is not None:
        if not isinstance(metadata, Mapping):
            raise ManualReceiptError("metadata must be a dictionary if provided.")
        entry["metadata"] = dict(metadata)

    receipts = load_manual_receipts(path)
    receipts[cleaned_message_id] = entry
    _write_atomic(path, receipts)
    log_event(
        LOGGER,
        "manual_receipt_saved",
        message_id=cleaned_message_id,
        reason=entry["reason"],
    )
    return entry


def _validate_manual_entry(message_id: str, entry: Mapping[str, Any]) -> dict[str, Any]:
    validated_entry: dict[str, Any] = {
        "message_id": message_id,
        "reason": str(entry.get("reason", "")),
    }
    if "metadata" in entry and isinstance(entry["metadata"], Mapping):
        validated_entry["metadata"] = dict(entry["metadata"])
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

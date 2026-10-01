"""Local, ignored cache for parsed historical receipt ingestions."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any

from src.observability.logging import log_event


LOGGER = logging.getLogger("splitwise_funnel.app.receipt_cache")
CACHE_VERSION = 1


class ReceiptCacheError(ValueError):
    """Raised when a local ingestion cache is missing or unsafe to use."""


def receipt_cache_path(
    cache_directory: Path,
    start_on: str,
    end_on: str,
    vendor_ids: Sequence[str],
) -> Path:
    """Return a stable local path for one exact historical receipt selection."""
    selection = _normalized_selection(start_on, end_on, vendor_ids)
    digest = hashlib.sha256(
        json.dumps(selection, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return cache_directory / f"receipt_ingestion_{digest}.json"


def load_cached_ingestion(
    cache_directory: Path,
    start_on: str,
    end_on: str,
    vendor_ids: Sequence[str],
) -> list[dict[str, Any]] | None:
    """Load parsed local records only when they match the full requested selection."""
    expected_selection = _normalized_selection(start_on, end_on, vendor_ids)
    cache_path = receipt_cache_path(cache_directory, start_on, end_on, vendor_ids)
    if not cache_path.is_file():
        log_event(LOGGER, "receipt_cache_miss", status="not_found")
        return None

    try:
        payload: object = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        log_event(LOGGER, "receipt_cache_miss", status="invalid")
        raise ReceiptCacheError("Local receipt cache could not be read safely.") from error

    if not isinstance(payload, dict):
        raise ReceiptCacheError("Local receipt cache has an invalid root.")
    if payload.get("version") != CACHE_VERSION or payload.get("selection") != expected_selection:
        log_event(LOGGER, "receipt_cache_miss", status="selection_mismatch")
        return None

    records = payload.get("records")
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise ReceiptCacheError("Local receipt cache has invalid receipt records.")
    log_event(LOGGER, "receipt_cache_hit", record_count=len(records))
    return records


def save_ingestion(
    cache_directory: Path,
    start_on: str,
    end_on: str,
    vendor_ids: Sequence[str],
    records: Sequence[dict[str, Any]],
) -> Path:
    """Atomically save parsed receipt records without raw email bodies."""
    selection = _normalized_selection(start_on, end_on, vendor_ids)
    cache_path = receipt_cache_path(cache_directory, start_on, end_on, vendor_ids)
    payload = {
        "version": CACHE_VERSION,
        "selection": selection,
        "created_at": datetime.now(UTC).isoformat(),
        "records": list(records),
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    cache_directory.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=cache_directory,
            prefix=f".{cache_path.name}.",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as cache_file:
            cache_file.write(serialized)
        os.replace(temporary_path, cache_path)
    except OSError as error:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        log_event(LOGGER, "receipt_cache_save_failed", status="os_error")
        raise ReceiptCacheError("Parsed receipt cache could not be saved locally.") from error

    log_event(LOGGER, "receipt_cache_saved", record_count=len(records))
    return cache_path


def _normalized_selection(
    start_on: str,
    end_on: str,
    vendor_ids: Sequence[str],
) -> dict[str, object]:
    if not vendor_ids or any(not isinstance(vendor_id, str) or not vendor_id for vendor_id in vendor_ids):
        raise ReceiptCacheError("Receipt cache requires at least one valid vendor id.")
    return {
        "start_on": start_on,
        "end_on": end_on,
        "vendor_ids": sorted(set(vendor_ids)),
    }


def clear_cached_ingestions(
    cache_directory: Path,
    manual_receipts_path: Path | None = None,
) -> int:
    """Remove all cached ingestion JSON files from the given directory and reset manual receipts if provided."""
    if manual_receipts_path is not None:
        try:
            manual_receipts_path.parent.mkdir(parents=True, exist_ok=True)
            manual_receipts_path.write_text("{}\n", encoding="utf-8")
        except OSError:
            pass
    if not cache_directory.is_dir():
        return 0
    count = 0
    for path in cache_directory.glob("*.json"):
        if path.is_file():
            try:
                path.unlink()
                count += 1
            except OSError:
                pass
    log_event(LOGGER, "receipt_cache_cleared", cleared_count=count)
    return count


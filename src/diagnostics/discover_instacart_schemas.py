"""Discover privacy-safe structural variants in historical Instacart Walmart receipts."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import time
from typing import Any

from src.cli import RECEIPT_SIGNATURES, _default_oauth_directory
from src.gmail.auth import OAuthPaths, build_gmail_service
from src.gmail.client import (
    GmailRateLimitError,
    _execute_gmail_request,
    _extract_supported_body,
    _message_headers,
)
from src.gmail.receipt_filters import build_vendor_subject_queries, matches_receipt_signature
from src.observability.logging import configure_logging, log_event
from src.parsers.receipts import ReceiptParseError, parse_receipt_email


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIRECTORY = PROJECT_ROOT / "data" / "schema_discovery" / "instacart_walmart"
VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"})
VOLATILE_ATTRIBUTES = frozenset({"href", "id", "src", "style", "title", "value"})
SCHEMA_SIGNAL_LABELS = {
    "item-row": "item_rows",
    "item-name": "item_names",
    "item-price": "item_prices",
    "order id:": "order_id_label",
    "was placed on": "purchase_date_label",
    "total charged": "total_charged_label",
    "paypal": "paypal_evidence",
    "visa": "visa_evidence",
    "refunded": "refund_evidence",
}


class _SchemaParser(HTMLParser):
    """Convert HTML into a value-free structural token stream."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[str] = []
        self._open_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.casefold()
        attribute_names: list[str] = []
        class_tokens: list[str] = []
        for key, value in attrs:
            normalized_key = key.casefold()
            if normalized_key in VOLATILE_ATTRIBUTES:
                continue
            if normalized_key == "class":
                class_tokens.extend(
                    token.casefold() for token in (value or "").split() if token
                )
                continue
            attribute_names.append(normalized_key)
        attributes = ",".join(sorted(set(attribute_names)))
        classes = ".".join(sorted(set(class_tokens)))
        descriptor = normalized_tag
        if classes:
            descriptor = f"{descriptor}.{classes}"
        if attributes:
            descriptor = f"{descriptor}[{attributes}]"
        self.tokens.append(f"open:{descriptor}")
        if normalized_tag not in VOID_TAGS:
            self._open_tags.append(normalized_tag)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        for index in range(len(self._open_tags) - 1, -1, -1):
            if self._open_tags[index] != normalized_tag:
                continue
            for closing_tag in reversed(self._open_tags[index:]):
                self.tokens.append(f"close:{closing_tag}")
            del self._open_tags[index:]
            return


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse bounded, resumable historical schema discovery options."""
    today = date.today()
    parser = argparse.ArgumentParser(
        description="Discover structural variants in Instacart Walmart receipt emails."
    )
    parser.add_argument("--start-on", default=_months_before(today, 8).isoformat())
    parser.add_argument("--end-on", default=today.isoformat())
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_OUTPUT_DIRECTORY / "logs")
    parser.add_argument("--oauth-dir", type=Path, default=_default_oauth_directory())
    parser.add_argument("--batch-size", type=_positive_int, default=10)
    parser.add_argument("--pause-seconds", type=_nonnegative_float, default=3.0)
    parser.add_argument("--max-messages", type=_positive_int)
    parser.add_argument("--refresh-message-list", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Fetch one historical selection once and save schema-only analysis artifacts."""
    arguments = parse_arguments(argv)
    logger = configure_logging(arguments.log_dir)
    selection = {
        "start_on": arguments.start_on,
        "end_on": arguments.end_on,
        "vendor_id": "walmart_online",
        "max_messages": arguments.max_messages,
    }
    progress_path = _progress_path(arguments.output, selection)
    try:
        service: Any | None = None
        progress = _load_progress(progress_path, selection)
        if progress is not None:
            _normalize_existing_observations(progress["observations"])
            _write_json(progress_path, progress)
        if progress is None or arguments.refresh_message_list:
            service = build_gmail_service(OAuthPaths.from_directory(arguments.oauth_dir))
            message_ids = list_matching_message_ids(
                service, arguments.start_on, arguments.end_on, arguments.max_messages
            )
            progress = {"selection": selection, "message_ids": message_ids, "observations": []}
            _write_json(progress_path, progress)
            log_event(logger, "schema_discovery_message_list_saved", message_count=len(message_ids))

        processed_ids = {
            observation["message_id"]
            for observation in progress["observations"]
            if isinstance(observation, dict) and isinstance(observation.get("message_id"), str)
        }
        remaining_ids = [message_id for message_id in progress["message_ids"] if message_id not in processed_ids]
        if remaining_ids and service is None:
            service = build_gmail_service(OAuthPaths.from_directory(arguments.oauth_dir))
        for index, message_id in enumerate(remaining_ids, start=1):
            if service is None:
                raise ValueError("Gmail service is unavailable for schema discovery.")
            observation = observe_message(service, message_id)
            progress["observations"].append(observation)
            _write_json(progress_path, progress)
            if index % arguments.batch_size == 0 or index == len(remaining_ids):
                write_schema_outputs(arguments.output, progress["observations"])
                log_event(
                    logger,
                    "schema_discovery_batch_saved",
                    processed_count=len(progress["observations"]),
                    remaining_count=len(remaining_ids) - index,
                )
                if index < len(remaining_ids) and arguments.pause_seconds:
                    time.sleep(arguments.pause_seconds)
    except GmailRateLimitError:
        if "progress" in locals():
            write_schema_outputs(arguments.output, progress["observations"])
        log_event(logger, "schema_discovery_rate_limited", status="rate_limited")
        return 1
    except (OSError, ValueError) as error:
        log_event(logger, "schema_discovery_failed", error_type=type(error).__name__)
        return 2

    write_schema_outputs(arguments.output, progress["observations"])
    log_event(logger, "schema_discovery_completed", observation_count=len(progress["observations"]))
    return 0


def list_matching_message_ids(
    service: Any,
    start_on: str,
    end_on: str,
    max_messages: int | None,
) -> list[str]:
    """List the historical Instacart Walmart message ids using one narrow query."""
    messages = service.users().messages()
    message_ids: list[str] = []
    for query in build_vendor_subject_queries(
        "walmart_online", start_on, end_on, RECEIPT_SIGNATURES
    ):
        page_token: str | None = None
        while max_messages is None or len(message_ids) < max_messages:
            response = _execute_gmail_request(
                messages.list(userId="me", q=query, pageToken=page_token, maxResults=100)
            )
            refs = response.get("messages", [])
            if not isinstance(refs, list):
                raise ValueError("Gmail message listing returned an invalid messages collection.")
            for reference in refs:
                if not isinstance(reference, Mapping):
                    raise ValueError("Gmail message listing contains an invalid message reference.")
                message_id = reference.get("id")
                if not isinstance(message_id, str) or not message_id:
                    raise ValueError("Gmail message listing contains a message without an id.")
                if message_id not in message_ids:
                    message_ids.append(message_id)
                if max_messages is not None and len(message_ids) >= max_messages:
                    break
            next_page_token = response.get("nextPageToken")
            if next_page_token is None:
                break
            if not isinstance(next_page_token, str) or not next_page_token:
                raise ValueError("Gmail message listing returned an invalid next page token.")
            page_token = next_page_token
    return message_ids


def observe_message(service: Any, message_id: str) -> dict[str, object]:
    """Return one value-free schema observation for a single receipt message."""
    message = _execute_gmail_request(
        service.users().messages().get(userId="me", id=message_id, format="full")
    )
    sender, subject = _message_headers(message)
    signature = RECEIPT_SIGNATURES["walmart_online"]
    if not matches_receipt_signature(sender, subject, signature):
        return {"message_id": message_id, "status": "signature_mismatch"}
    try:
        html = _extract_supported_body(message)
    except ValueError:
        return {"message_id": message_id, "status": "unsupported_body"}
    observation = schema_observation(message_id, html)
    try:
        parse_receipt_email("walmart_online", html, fallback_receipt_id=message_id)
        observation["parser_outcome"] = "parsed"
    except ReceiptParseError as error:
        observation["parser_outcome"] = "rejected"
        observation["failure_code"] = failure_code(error)
    return observation


def schema_observation(message_id: str, html: str) -> dict[str, object]:
    """Produce a schema-only observation with no rendered receipt text or values."""
    parser = _SchemaParser()
    parser.feed(html)
    parser.close()
    shape = _canonical_schema_shape(parser.tokens)
    fingerprint = hashlib.sha256(shape.encode("utf-8")).hexdigest()[:16]
    lowered_html = html.casefold()
    signals = {
        name: label.casefold() in lowered_html
        for label, name in SCHEMA_SIGNAL_LABELS.items()
    }
    return {
        "message_id": message_id,
        "status": "observed",
        "schema_fingerprint": fingerprint,
        "schema_shape": shape,
        "signals": signals,
    }


def _canonical_schema_shape(tokens: Sequence[str]) -> str:
    """Collapse repeated receipt rows into one template-level structural signature."""
    return " ".join(sorted({token for token in tokens if token.startswith("open:")}))


def _normalize_existing_observations(observations: Sequence[object]) -> None:
    """Migrate earlier exact-tree observations to repeat-insensitive template fingerprints."""
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        shape = observation.get("schema_shape")
        if not isinstance(shape, str):
            continue
        normalized_shape = _canonical_schema_shape(shape.split())
        observation["schema_shape"] = normalized_shape
        observation["schema_fingerprint"] = hashlib.sha256(
            normalized_shape.encode("utf-8")
        ).hexdigest()[:16]


def failure_code(error: ReceiptParseError) -> str:
    """Map parser failures to stable, value-free schema categories."""
    message = str(error).casefold()
    if "item row" in message or "item rows" in message:
        return "missing_finalized_items"
    if "purchase date" in message:
        return "missing_purchase_date"
    if "total" in message:
        return "missing_final_total"
    if "order id" in message:
        return "missing_order_id"
    return "unsupported_structure"


def build_schema_summary(observations: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Aggregate parser outcomes and structural variants without source identifiers."""
    variants: dict[str, dict[str, object]] = {}
    parser_outcomes: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    for observation in observations:
        status = observation.get("status")
        if isinstance(status, str):
            status_counts[status] += 1
        outcome = observation.get("parser_outcome")
        if isinstance(outcome, str):
            parser_outcomes[outcome] += 1
        fingerprint = observation.get("schema_fingerprint")
        shape = observation.get("schema_shape")
        signals = observation.get("signals")
        if not isinstance(fingerprint, str) or not isinstance(shape, str) or not isinstance(signals, Mapping):
            continue
        variant = variants.setdefault(
            fingerprint,
            {
                "schema_fingerprint": fingerprint,
                "schema_shape": shape,
                "signals": dict(signals),
                "message_count": 0,
                "parser_outcomes": Counter(),
                "failure_codes": Counter(),
            },
        )
        variant["message_count"] += 1
        if isinstance(outcome, str):
            variant["parser_outcomes"][outcome] += 1
        failure = observation.get("failure_code")
        if isinstance(failure, str):
            variant["failure_codes"][failure] += 1

    normalized_variants = []
    for variant in variants.values():
        normalized_variants.append(
            {
                **variant,
                "parser_outcomes": dict(variant["parser_outcomes"]),
                "failure_codes": dict(variant["failure_codes"]),
            }
        )
    normalized_variants.sort(key=lambda variant: (-variant["message_count"], variant["schema_fingerprint"]))
    return {
        "message_count": len(observations),
        "schema_variant_count": len(normalized_variants),
        "status_counts": dict(status_counts),
        "parser_outcomes": dict(parser_outcomes),
        "variants": normalized_variants,
    }


def write_schema_outputs(output_directory: Path, observations: Sequence[Mapping[str, object]]) -> None:
    """Persist schema reports only; raw source bodies never leave memory."""
    processed_directory = output_directory / "processed"
    summary = build_schema_summary(observations)
    _write_json(processed_directory / "schema_summary.json", {
        key: value for key, value in summary.items() if key != "variants"
    })
    _write_json(processed_directory / "schema_variants.json", {"variants": summary["variants"]})
    report = "\n".join(
        [
            "# Instacart Walmart Receipt Schema Discovery",
            "",
            f"- Messages observed: {summary['message_count']}",
            f"- Schema variants: {summary['schema_variant_count']}",
            f"- Parser outcomes: {json.dumps(summary['parser_outcomes'], sort_keys=True)}",
            f"- Non-schema statuses: {json.dumps(summary['status_counts'], sort_keys=True)}",
            "",
            "This report contains structural schema evidence only. It excludes raw email bodies, product names, totals, card data, and rendered receipt text.",
            "",
        ]
    )
    _write_text(processed_directory / "DATASET_SUMMARY.md", report)


def _progress_path(output_directory: Path, selection: Mapping[str, object]) -> Path:
    digest = hashlib.sha256(
        json.dumps(selection, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return output_directory / "raw" / f"message_inventory_{digest}.json"


def _load_progress(path: Path, selection: Mapping[str, object]) -> dict[str, list[object]] | None:
    if not path.is_file():
        return None
    try:
        progress = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Schema discovery progress is invalid.") from error
    if not isinstance(progress, dict) or progress.get("selection") != selection:
        raise ValueError("Schema discovery progress does not match this selection.")
    message_ids = progress.get("message_ids")
    observations = progress.get("observations")
    if not isinstance(message_ids, list) or not all(isinstance(item, str) for item in message_ids):
        raise ValueError("Schema discovery progress has invalid message ids.")
    if not isinstance(observations, list):
        raise ValueError("Schema discovery progress has invalid observations.")
    return {"selection": selection, "message_ids": message_ids, "observations": observations}


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    _write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _months_before(value: date, months: int) -> date:
    month_offset = value.year * 12 + value.month - 1 - months
    year, month_index = divmod(month_offset, 12)
    month = month_index + 1
    return date(year, month, min(value.day, 28))


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("Value must be positive.")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("Value must be non-negative.")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())

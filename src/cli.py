"""Command-line entrypoint for local Gmail receipt allocation."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable, Sequence
from typing import Final

from src.app.config import (
    ConfigError,
    append_participant_and_payment_mapping,
    append_payment_mapping,
    load_config,
)
from src.app.results import ResultError, write_result_files
from src.app.workflow import (
    UnknownPaymentMappingError,
    WorkflowError,
    process_receipt_emails,
)
from src.gmail.auth import GmailAuthError, OAuthPaths, build_gmail_service
from src.gmail.client import ReceiptEmail, fetch_receipt_emails
from src.observability.logging import configure_logging, log_event


LOGGER = logging.getLogger("splitwise_funnel.cli")


DEFAULT_VENDOR_IDS: Final[tuple[str, str]] = (
    "walmart_online",
    "costco_same_day",
)

RECEIPT_SIGNATURES: Final[dict[str, dict[str, str | tuple[str, ...]]]] = {
    "walmart_online": {
        "sender": "orders@instacart.com",
        "subject": (
            "Your Instacart order receipt",
            "Instacart Order Receipt",
        ),
    },
    "costco_same_day": {
        "sender": "no-reply@costco.com",
        "subject": "Your Costco order receipt",
    },
}


def choose_unknown_payer(
    participant_ids: Sequence[str],
    input_func: Callable[[str], str] = input,
    output_func: Callable[[str], None] = print,
) -> tuple[str, bool]:
    """Select an existing participant or create a normalized local identifier."""
    for index, participant_id in enumerate(participant_ids, start=1):
        output_func(f"{index}. {participant_id}")

    while True:
        answer: str = input_func("Choose payer number or enter new: Full Name: ").strip()
        if answer.casefold().startswith("new:"):
            new_id: str = _participant_id_from_name(answer[4:])
            if new_id:
                return new_id, True
        elif answer.isdigit():
            selection: int = int(answer)
            if 1 <= selection <= len(participant_ids):
                return participant_ids[selection - 1], False
        output_func("Invalid payer choice. Enter a listed number or new: Full Name.")


def process_emails_with_unknown_payment_resolution(
    emails: Sequence[ReceiptEmail],
    config_path: Path,
    signatures: dict[str, dict[str, str]],
    manual_payer_id: str | None = None,
    input_func: Callable[[str], str] = input,
    output_func: Callable[[str], None] = print,
) -> list[dict[str, object]]:
    """Process receipt emails, persistently resolving unmapped cards when needed."""
    config: dict[str, object] = load_config(config_path)
    results: list[dict[str, object]] = []
    for email in emails:
        try:
            result = process_receipt_emails(
                [email], config, signatures, manual_payer_id=manual_payer_id
            )[0]
        except UnknownPaymentMappingError as error:
            if manual_payer_id is not None:
                raise
            output_func("Unknown payment mapping. Select the payer for this receipt.")
            participant_ids = config.get("participant_ids")
            if not isinstance(participant_ids, list) or not all(
                isinstance(participant_id, str) for participant_id in participant_ids
            ):
                raise ConfigError("participant_ids must be a non-empty JSON array.")
            payer_id, creates_participant = choose_unknown_payer(
                participant_ids, input_func=input_func, output_func=output_func
            )
            if error.last_four is None:
                result = process_receipt_emails(
                    [email], config, signatures, manual_payer_id=payer_id
                )[0]
            elif creates_participant:
                config = append_participant_and_payment_mapping(
                    config_path, error.last_four, payer_id
                )
                result = process_receipt_emails([email], config, signatures)[0]
            else:
                config = append_payment_mapping(config_path, error.last_four, payer_id)
                result = process_receipt_emails([email], config, signatures)[0]
        results.append(result)
    return results


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the explicit, local-only receipt processing arguments."""
    parser = argparse.ArgumentParser(
        description="Create local Splitwise-ready receipt summaries from Gmail receipts."
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--start-on", required=True, metavar="YYYY-MM-DD")
    parser.add_argument("--end-on", required=True, metavar="YYYY-MM-DD")
    parser.add_argument(
        "--vendors",
        choices=DEFAULT_VENDOR_IDS,
        nargs="+",
        default=list(DEFAULT_VENDOR_IDS),
    )
    parser.add_argument("--manual-payer")
    parser.add_argument("--oauth-dir", type=Path, default=_default_oauth_directory())
    parser.add_argument("--log-dir", type=Path, default=_default_log_directory())
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Fetch, process, and write receipt results without retaining email bodies."""
    arguments = parse_arguments(argv)
    configure_logging(arguments.log_dir)
    log_event(LOGGER, "cli_run_started", vendor_count=len(arguments.vendors))
    try:
        service = build_gmail_service(OAuthPaths.from_directory(arguments.oauth_dir))
        emails = fetch_receipt_emails(
            service=service,
            vendor_ids=arguments.vendors,
            starts_on=arguments.start_on,
            ends_on=arguments.end_on,
            signatures=RECEIPT_SIGNATURES,
        )
        if not emails:
            log_event(LOGGER, "cli_run_failed", status="no_matching_receipts")
            raise WorkflowError("No matching receipt emails were found.")

        results = process_emails_with_unknown_payment_resolution(
            emails,
            arguments.config,
            RECEIPT_SIGNATURES,
            manual_payer_id=arguments.manual_payer,
        )
        output_paths = _write_results(results, arguments.output)
    except (ConfigError, GmailAuthError, ResultError, ValueError, WorkflowError, OSError):
        log_event(LOGGER, "cli_run_failed", status="processing_error")
        print(
            "Error: receipt processing could not complete. Verify local setup, dates, "
            "mappings, and receipt formats.",
            file=sys.stderr,
        )
        return 2

    log_event(LOGGER, "cli_run_completed", status="completed", receipt_count=len(output_paths))
    print(f"Wrote {len(output_paths)} receipt result(s).")
    for json_path, markdown_path in output_paths:
        print(json_path)
        print(markdown_path)
    return 0


def _default_oauth_directory() -> Path:
    local_app_data: str | None = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "SplitwiseFunnel" / "oauth"
    return Path.home() / "AppData" / "Local" / "SplitwiseFunnel" / "oauth"


def _default_log_directory() -> Path:
    local_app_data: str | None = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "SplitwiseFunnel" / "logs"
    return Path.home() / "AppData" / "Local" / "SplitwiseFunnel" / "logs"


def _write_results(
    results: Sequence[dict[str, object]], output_directory: Path
) -> list[tuple[Path, Path]]:
    written_paths: list[tuple[Path, Path]] = []
    run_timestamp: str = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for index, result in enumerate(results, start=1):
        receipt: object = result.get("receipt")
        if not isinstance(receipt, dict):
            raise ResultError("Receipt result is missing an auditable receipt object.")
        receipt_id: object = receipt.get("receipt_id")
        if not isinstance(receipt_id, str) or not receipt_id.strip():
            raise ResultError("Receipt result is missing a receipt identifier.")
        timestamp = _available_timestamp(
            output_directory, run_timestamp, receipt_id, index
        )
        written_paths.append(write_result_files(result, output_directory, timestamp))
    return written_paths


def _available_timestamp(
    output_directory: Path, run_timestamp: str, receipt_id: str, index: int
) -> str:
    safe_receipt_id: str = _filesystem_safe_identifier(receipt_id)
    base_timestamp: str = f"{run_timestamp}_{index:03d}_{safe_receipt_id}"
    candidate: str = base_timestamp
    suffix: int = 1
    while _result_pair_exists(output_directory, candidate):
        suffix += 1
        candidate = f"{base_timestamp}_{suffix}"
    return candidate


def _result_pair_exists(output_directory: Path, timestamp: str) -> bool:
    return (
        (output_directory / f"receipt_result_{timestamp}.json").exists()
        or (output_directory / f"receipt_summary_{timestamp}.md").exists()
    )


def _filesystem_safe_identifier(value: str) -> str:
    normalized: str = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return normalized[:80] or "receipt"


def _participant_id_from_name(value: str) -> str:
    normalized: str = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    return normalized


if __name__ == "__main__":
    raise SystemExit(main())

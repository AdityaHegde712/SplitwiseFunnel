"""Run a bounded receipt diagnostic without changing household mappings."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from src.app.config import ConfigError, load_config
from src.app.results import ResultError, write_result_files
from src.app.workflow import UnknownPaymentMappingError, WorkflowError, process_receipt_emails
from src.cli import DEFAULT_VENDOR_IDS, RECEIPT_SIGNATURES, _default_oauth_directory
from src.gmail.auth import GmailAuthError, OAuthPaths, build_gmail_service
from src.gmail.client import ReceiptEmail, fetch_receipt_emails
from src.gmail.receipt_filters import matches_receipt_signature
from src.observability.logging import configure_logging, log_event


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse explicit local-only diagnostic parameters."""
    parser = argparse.ArgumentParser(description="Create safe backend diagnostic artifacts.")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--log-dir", required=True, type=Path)
    parser.add_argument("--start-on", required=True, metavar="YYYY-MM-DD")
    parser.add_argument("--end-on", required=True, metavar="YYYY-MM-DD")
    parser.add_argument("--vendors", choices=DEFAULT_VENDOR_IDS, nargs="+", default=list(DEFAULT_VENDOR_IDS))
    parser.add_argument("--manual-payer")
    parser.add_argument("--oauth-dir", type=Path, default=_default_oauth_directory())
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Fetch and process one range, retaining safe artifacts but no email bodies."""
    arguments = parse_arguments(argv)
    logger = configure_logging(arguments.log_dir)
    manifest_path = arguments.output / "run_manifest.json"
    manifest: dict[str, Any] = {
        "run_started_at": datetime.now(UTC).isoformat(),
        "start_on": arguments.start_on,
        "end_on": arguments.end_on,
        "vendors": arguments.vendors,
        "raw_email_bodies_persisted": False,
        "manual_payer_supplied": arguments.manual_payer is not None,
        "receipt_attempts": [],
    }
    _write_manifest(manifest_path, manifest)
    log_event(logger, "backend_diagnostic_started", vendor_count=len(arguments.vendors))
    try:
        service = build_gmail_service(OAuthPaths.from_directory(arguments.oauth_dir))
        emails = fetch_receipt_emails(
            service=service,
            vendor_ids=arguments.vendors,
            starts_on=arguments.start_on,
            ends_on=arguments.end_on,
            signatures=RECEIPT_SIGNATURES,
        )
        config = load_config(arguments.config)
    except (ConfigError, GmailAuthError, ValueError, OSError) as error:
        manifest["status"] = "setup_or_fetch_failed"
        manifest["error_type"] = type(error).__name__
        _write_manifest(manifest_path, manifest)
        log_event(logger, "backend_diagnostic_failed", error_type=type(error).__name__)
        return 2

    manifest["fetched_receipt_count"] = len(emails)
    if not emails:
        manifest["status"] = "no_matching_receipts"
        manifest["completed_receipt_count"] = 0
        _write_manifest(manifest_path, manifest)
        log_event(logger, "backend_diagnostic_completed", status="no_matching_receipts")
        return 1

    results_directory = arguments.output / "results"
    for index, email in enumerate(emails, start=1):
        attempt: dict[str, Any] = {"source_index": index, "vendor": _vendor_for_email(email)}
        try:
            result = process_receipt_emails(
                [email],
                config,
                RECEIPT_SIGNATURES,
                manual_payer_id=arguments.manual_payer,
            )[0]
            write_result_files(result, results_directory, _timestamp_for(index))
            attempt["status"] = "completed"
        except UnknownPaymentMappingError as error:
            attempt["status"] = "needs_payment_mapping"
            attempt["payment_last_four"] = error.last_four
        except (ConfigError, ResultError, ValueError, WorkflowError) as error:
            attempt["status"] = "failed"
            attempt["error_type"] = type(error).__name__
        manifest["receipt_attempts"].append(attempt)
        _write_manifest(manifest_path, manifest)

    completed_count = sum(item["status"] == "completed" for item in manifest["receipt_attempts"])
    manifest["status"] = "completed" if completed_count == len(emails) else "completed_with_findings"
    manifest["completed_receipt_count"] = completed_count
    _write_manifest(manifest_path, manifest)
    log_event(
        logger,
        "backend_diagnostic_completed",
        fetched_receipt_count=len(emails),
        completed_receipt_count=completed_count,
    )
    return 0 if manifest["status"] == "completed" else 1


def _timestamp_for(index: int) -> str:
    return f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{index:03d}"


def _vendor_for_email(email: ReceiptEmail) -> str:
    for vendor_id, signature in RECEIPT_SIGNATURES.items():
        if matches_receipt_signature(email.sender, email.subject, signature):
            return vendor_id
    return "unknown"


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())

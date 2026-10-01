"""Application workflow for turning validated receipt emails into split results."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import logging
from pathlib import Path
from typing import Any

from src.app.config import ConfigError, resolve_payer
from src.app.manual_receipts import (
    DEFAULT_MANUAL_RECEIPTS_PATH,
    load_manual_receipts,
    save_manual_receipt,
)
from src.app.refund_links import DEFAULT_REFUND_LINKS_PATH, load_refund_links
from src.app.results import ResultError, build_result
from src.domain.allocate import (
    AllocationError,
    allocate_receipt,
    get_present_participants,
    _split_cents,
)
from src.gmail.client import ReceiptEmail
from src.gmail.receipt_filters import matches_receipt_signature
from src.parsers.receipts import (
    ReceiptParseError,
    parse_instacart_refund_email,
    parse_receipt_email,
)
from src.observability.logging import log_event


LOGGER = logging.getLogger("splitwise_funnel.app.workflow")


class ProcessedReceipts(list):
    """List of reconciled receipt results with quarantined manual receipts metadata."""

    def __init__(
        self,
        results: Sequence[dict[str, Any]] = (),
        manual_receipts: Sequence[dict[str, Any]] = (),
    ) -> None:
        super().__init__(results)
        self.manual_receipts: list[dict[str, Any]] = list(manual_receipts)


class WorkflowError(ValueError):
    """Raised when a receipt email cannot safely become an allocation result."""


class UnknownPaymentMappingError(WorkflowError):
    """Raised when a parsed payment card has no local payer mapping."""

    def __init__(
        self,
        retailer: str,
        last_four: str | None,
        receipt_id: str | None = None,
        purchase_date: str | None = None,
        payment_method: str | None = None,
        email_sender: str | None = None,
        email_subject: str | None = None,
        message_id: str | None = None,
        amount_cents: int | None = None,
    ) -> None:
        self.retailer: str = retailer
        self.last_four: str | None = last_four
        self.receipt_id: str | None = receipt_id
        self.purchase_date: str | None = purchase_date
        self.payment_method: str | None = payment_method
        self.email_sender: str | None = email_sender
        self.email_subject: str | None = email_subject
        self.message_id: str | None = message_id
        self.amount_cents: int | None = amount_cents
        super().__init__("Receipt payment has no configured payer mapping.")


class ReceiptRefundLinkRequiredError(WorkflowError):
    """Raised when an unlinked refund is detected during receipt processing."""

    def __init__(
        self,
        refund: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
        retailer: str = "walmart_online",
    ) -> None:
        self.refund: dict[str, Any] = dict(refund)
        self.candidates: list[dict[str, Any]] = [dict(c) for c in candidates]
        self.retailer: str = retailer
        super().__init__("A refund needs an original receipt selection before this run can finish.")


class ReceiptReviewRequiredError(WorkflowError):
    """Raised when a separately identified receipt cannot safely be processed."""

    def __init__(
        self,
        retailer: str,
        reason_code: str,
        email_sender: str,
        email_subject: str,
        message_id: str | None = None,
        failure_detail: str | None = None,
    ) -> None:
        self.retailer: str = retailer
        self.reason_code: str = reason_code
        self.email_sender: str = email_sender
        self.email_subject: str = email_subject
        self.message_id: str | None = message_id
        self.failure_detail: str | None = failure_detail
        super().__init__("Receipt requires manual review before it can be processed.")




def process_receipt_emails(
    emails: Sequence[ReceiptEmail],
    config: Mapping[str, Any],
    signatures: Mapping[str, Mapping[str, object]],
    manual_payer_id: str | None = None,
    refund_links: Mapping[str, Mapping[str, Any]] | None = None,
    refund_links_path: Path | None = None,
    manual_receipts: Mapping[str, Any] | None = None,
    manual_receipts_path: Path | None = None,
    auto_quarantine: bool = False,
) -> ProcessedReceipts:
    """Parse receipt emails and process the resulting local records."""
    records = ingest_receipt_emails(emails, signatures)
    return process_cached_receipts(
        records,
        config,
        manual_payer_id=manual_payer_id,
        refund_links=refund_links,
        refund_links_path=refund_links_path,
        manual_receipts=manual_receipts,
        manual_receipts_path=manual_receipts_path,
        auto_quarantine=auto_quarantine,
    )



def ingest_receipt_emails(
    emails: Sequence[ReceiptEmail],
    signatures: Mapping[str, Mapping[str, object]],
) -> list[dict[str, Any]]:
    """Create local parsed receipt records without retaining raw email bodies."""
    records: list[dict[str, Any]] = []
    for email in emails:
        vendor_id = _vendor_id_for_email(email, signatures)
        source = {
            "message_id": email.message_id,
            "vendor_id": vendor_id,
            "email_sender": email.sender,
            "email_subject": email.subject,
        }
        try:
            receipt = parse_receipt_email(
                vendor_id,
                email.html,
                fallback_receipt_id=email.message_id,
            )
        except ReceiptParseError as parse_error:
            failure_detail: str = str(parse_error)
            if vendor_id == "walmart_online":
                try:
                    refund = parse_instacart_refund_email(
                        email.html,
                        fallback_refund_id=email.message_id,
                    )
                    records.append(
                        {
                            "status": "refund_pending_link",
                            "source": source,
                            "refund": {
                                "refund_id": refund["refund_id"],
                                "retailer": "walmart_online",
                                "refund_date": refund["refund_date"],
                                "refund_amount_cents": refund["refund_amount_cents"],
                                "source": source,
                            },
                        }
                    )
                    log_event(
                        LOGGER,
                        "receipt_ingestion_item_refund_detected",
                        correlation_id=email.message_id,
                        vendor_id=vendor_id,
                    )
                    continue
                except ReceiptParseError:
                    pass
            records.append(
                {
                    "status": "review_required",
                    "reason_code": "unsupported_receipt_format",
                    "source": source,
                    "message_id": email.message_id,
                    "failure_detail": failure_detail,
                }
            )
            log_event(
                LOGGER,
                "receipt_ingestion_item_requires_review",
                correlation_id=email.message_id,
                vendor_id=vendor_id,
                message_id=email.message_id,
                failure_detail=failure_detail,
            )
            continue
        records.append({"status": "parsed", "source": source, "receipt": receipt})
        log_event(
            LOGGER,
            "receipt_ingestion_item_parsed",
            correlation_id=email.message_id,
            vendor_id=vendor_id,
        )
    log_event(LOGGER, "receipt_ingestion_completed", receipt_count=len(records))
    return records


def process_cached_receipts(
    records: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    manual_payer_id: str | None = None,
    refund_links: Mapping[str, Mapping[str, Any]] | None = None,
    refund_links_path: Path | None = None,
    manual_receipts: Mapping[str, Any] | None = None,
    manual_receipts_path: Path | None = None,
    auto_quarantine: bool = False,
) -> ProcessedReceipts:
    """Allocate previously parsed local records without querying Gmail or reparsing email bodies."""
    log_event(LOGGER, "receipt_processing_started", receipt_count=len(records))

    if manual_receipts is not None:
        active_manual_receipts = dict(manual_receipts)
    elif manual_receipts_path is not None:
        active_manual_receipts = load_manual_receipts(manual_receipts_path)
    elif DEFAULT_MANUAL_RECEIPTS_PATH.is_file():
        active_manual_receipts = load_manual_receipts(DEFAULT_MANUAL_RECEIPTS_PATH)
    else:
        active_manual_receipts = {}

    collected_manual_receipts: list[dict[str, Any]] = []

    for record in records:
        if record.get("status") == "review_required":
            source = _cached_source(record)
            message_id = record.get("message_id") or source.get("message_id")
            if message_id in active_manual_receipts:
                entry = active_manual_receipts[message_id]
                metadata = entry.get("metadata", {}) if isinstance(entry, Mapping) else {}
                reason = (
                    entry.get("reason")
                    if isinstance(entry, Mapping) and entry.get("reason")
                    else (_cached_reason_code(record) or "quarantined")
                )
                collected_manual_receipts.append(
                    {
                        "message_id": message_id,
                        "receipt_id": metadata.get("receipt_id") or record.get("receipt_id") or message_id,
                        "retailer": metadata.get("retailer") or source["vendor_id"],
                        "purchase_date": metadata.get("purchase_date") or record.get("purchase_date") or "Unknown",
                        "reason": reason,
                    }
                )
                log_event(
                    LOGGER,
                    "receipt_processing_item_quarantined",
                    correlation_id=message_id,
                    vendor_id=source["vendor_id"],
                    reason=reason,
                )
                continue

            if auto_quarantine:
                reason = _cached_reason_code(record) or "unsupported_receipt_format"
                quarantine_entry = {
                    "message_id": message_id,
                    "receipt_id": record.get("receipt_id") or message_id,
                    "retailer": source["vendor_id"],
                    "purchase_date": record.get("purchase_date") or "Unknown",
                    "reason": reason,
                }
                collected_manual_receipts.append(quarantine_entry)
                if manual_receipts_path is not None:
                    try:
                        save_manual_receipt(
                            manual_receipts_path,
                            message_id=message_id,
                            reason=reason,
                            metadata={
                                "retailer": quarantine_entry["retailer"],
                                "receipt_id": quarantine_entry["receipt_id"],
                                "purchase_date": quarantine_entry["purchase_date"],
                            },
                        )
                    except Exception:
                        pass
                log_event(
                    LOGGER,
                    "receipt_processing_item_auto_quarantined",
                    correlation_id=message_id,
                    vendor_id=source["vendor_id"],
                    reason=reason,
                )
                continue

            failure_detail = record.get("failure_detail")
            log_event(
                LOGGER,
                "receipt_processing_item_requires_review",
                correlation_id=message_id,
                vendor_id=source["vendor_id"],
                reason_code=_cached_reason_code(record),
                message_id=message_id,
                failure_detail=failure_detail,
            )
            raise ReceiptReviewRequiredError(
                retailer=source["vendor_id"],
                reason_code=_cached_reason_code(record),
                email_sender=source["email_sender"],
                email_subject=source["email_subject"],
                message_id=message_id,
                failure_detail=failure_detail,
            )


    parsed_records = [r for r in records if r.get("status") == "parsed"]
    refund_records = [r for r in records if r.get("status") == "refund_pending_link"]

    target_adjustments: dict[str, dict[str, Any]] = {}
    if refund_records:
        if refund_links is not None:
            active_links = dict(refund_links)
        elif refund_links_path is not None:
            active_links = load_refund_links(refund_links_path)
        elif DEFAULT_REFUND_LINKS_PATH.is_file():
            active_links = load_refund_links(DEFAULT_REFUND_LINKS_PATH)
        else:
            active_links = {}

        # Check for unlinked refunds
        for refund_record in refund_records:
            refund_info = refund_record.get("refund")
            if not isinstance(refund_info, Mapping):
                raise WorkflowError("Cached refund record has no valid refund details.")
            refund_id = refund_info.get("refund_id")
            if not isinstance(refund_id, str) or refund_id not in active_links:
                candidates: list[dict[str, Any]] = []
                ref_date = refund_info.get("refund_date")
                for p_rec in parsed_records:
                    rec_receipt = p_rec.get("receipt", {})
                    if rec_receipt.get("retailer") == "walmart_online":
                        p_date = rec_receipt.get("purchase_date")
                        if p_date and ref_date:
                            try:
                                if str(p_date) > str(ref_date):
                                    continue
                            except Exception:
                                pass
                        candidates.append(
                            {
                                "receipt_id": rec_receipt["receipt_id"],
                                "purchase_date": rec_receipt["purchase_date"],
                                "final_total_cents": rec_receipt["final_total_cents"],
                            }
                        )
                raise ReceiptRefundLinkRequiredError(
                    refund=refund_info,
                    candidates=candidates,
                    retailer=str(refund_info.get("retailer", "walmart_online")),
                )

        parsed_by_id = {
            r["receipt"]["receipt_id"]: r["receipt"]
            for r in parsed_records
            if "receipt" in r and isinstance(r["receipt"], Mapping) and "receipt_id" in r["receipt"]
        }

        for refund_record in refund_records:
            refund_info = refund_record["refund"]
            refund_id = refund_info["refund_id"]
            link = active_links[refund_id]
            target_receipt_id = link.get("original_receipt_id")
            if target_receipt_id not in parsed_by_id:
                source = _cached_source(refund_record)
                message_id = refund_record.get("message_id") or source.get("message_id")
                failure_detail = f"Original receipt {target_receipt_id} not found for refund {refund_id}"
                log_event(
                    LOGGER,
                    "receipt_processing_item_requires_review",
                    correlation_id=message_id,
                    vendor_id=str(refund_info.get("retailer", "walmart_online")),
                    reason_code="refund_target_receipt_not_found",
                    message_id=message_id,
                    failure_detail=failure_detail,
                )
                raise ReceiptReviewRequiredError(
                    retailer=str(refund_info.get("retailer", "walmart_online")),
                    reason_code="refund_target_receipt_not_found",
                    email_sender=source["email_sender"],
                    email_subject=source["email_subject"],
                    message_id=message_id,
                    failure_detail=failure_detail,
                )

            target_receipt = parsed_by_id[target_receipt_id]
            refund_amount = int(refund_info["refund_amount_cents"])
            adjustment_cents = -abs(refund_amount)
            purchase_date = str(target_receipt["purchase_date"])
            present_participants = get_present_participants(
                config["participant_ids"], purchase_date, config.get("absences", [])
            )
            if not present_participants:
                raise AllocationError("No present participants to receive refund adjustment.")
            refund_shares = _split_cents(adjustment_cents, present_participants)

            if target_receipt_id not in target_adjustments:
                target_adjustments[target_receipt_id] = {
                    "adjustment_cents": 0,
                    "details": [],
                    "shares_cents": {},
                }
            target_adjustments[target_receipt_id]["adjustment_cents"] += adjustment_cents
            target_adjustments[target_receipt_id]["details"].append(
                {
                    "refund_id": refund_id,
                    "refund_date": refund_info.get("refund_date"),
                    "refund_amount_cents": refund_amount,
                    "shares_cents": refund_shares,
                }
            )
            for p_id, p_share in refund_shares.items():
                target_adjustments[target_receipt_id]["shares_cents"][p_id] = (
                    target_adjustments[target_receipt_id]["shares_cents"].get(p_id, 0)
                    + p_share
                )

    results: list[dict[str, Any]] = []
    for record in parsed_records:
        source = _cached_source(record)
        vendor_id = source["vendor_id"]
        message_id = source["message_id"]

        receipt = _cached_receipt(record)
        if message_id in active_manual_receipts:
            entry = active_manual_receipts[message_id]
            metadata = entry.get("metadata", {}) if isinstance(entry, Mapping) else {}
            reason = (
                entry.get("reason")
                if isinstance(entry, Mapping) and entry.get("reason")
                else "quarantined_manual_entry"
            )
            collected_manual_receipts.append(
                {
                    "message_id": message_id,
                    "receipt_id": metadata.get("receipt_id") or str(receipt.get("receipt_id", message_id)),
                    "retailer": metadata.get("retailer") or vendor_id,
                    "purchase_date": metadata.get("purchase_date") or str(receipt.get("purchase_date", "Unknown")),
                    "reason": reason,
                }
            )
            log_event(
                LOGGER,
                "receipt_processing_item_quarantined",
                correlation_id=message_id,
                vendor_id=vendor_id,
                reason=reason,
            )
            continue

        log_event(
            LOGGER,
            "receipt_processing_item_started",
            correlation_id=source["message_id"],
            vendor_id=vendor_id,
        )
        try:
            payment_last_four: object = receipt.get("payment_last_four")
            last_four: str | None = (
                payment_last_four if isinstance(payment_last_four, str) else None
            )
            payer_id, payer_resolution = _resolve_email_payer(
                vendor_id, last_four, manual_payer_id, config
            )
            allocation: dict[str, Any] = allocate_receipt(receipt, config)
            receipt_id = str(receipt.get("receipt_id"))
            if receipt_id in target_adjustments:
                adj = target_adjustments[receipt_id]
                details = adj["details"][0] if len(adj["details"]) == 1 else adj["details"]
                result = build_result(
                    receipt=receipt,
                    payer_id=payer_id,
                    payer_resolution=payer_resolution,
                    allocation=allocation,
                    refund_adjustment_cents=adj["adjustment_cents"],
                    refund_details=details,
                    refund_shares_cents=adj["shares_cents"],
                )
            else:
                result = build_result(receipt, payer_id, payer_resolution, allocation)
            results.append(result)
            log_event(
                LOGGER,
                "receipt_processing_item_completed",
                correlation_id=source["message_id"],
                vendor_id=vendor_id,
                status="completed",
            )
        except UnknownPaymentMappingError as error:
            error.message_id = source["message_id"]
            error.receipt_id = _optional_string(receipt.get("receipt_id"))
            error.purchase_date = _optional_string(receipt.get("purchase_date"))
            error.payment_method = _optional_string(receipt.get("payment_method"))
            final_cents = receipt.get("final_total_cents")
            error.amount_cents = final_cents if isinstance(final_cents, int) else None
            error.email_sender = source["email_sender"]
            error.email_subject = source["email_subject"]
            log_event(
                LOGGER,
                "receipt_processing_item_needs_payer_resolution",
                correlation_id=source["message_id"],
                vendor_id=vendor_id,
            )
            raise
        except (AllocationError, ConfigError, ResultError) as error:
            failure_detail = str(error)
            log_event(
                LOGGER,
                "receipt_processing_item_failed",
                correlation_id=message_id,
                vendor_id=vendor_id,
                error_type=type(error).__name__,
            )
            if auto_quarantine:
                reason = "receipt_processing_error"
                quarantine_entry = {
                    "message_id": message_id,
                    "receipt_id": _optional_string(receipt.get("receipt_id")) or message_id,
                    "retailer": vendor_id,
                    "purchase_date": _optional_string(receipt.get("purchase_date")) or "Unknown",
                    "reason": reason,
                }
                collected_manual_receipts.append(quarantine_entry)
                if manual_receipts_path is not None:
                    try:
                        save_manual_receipt(
                            manual_receipts_path,
                            message_id=message_id,
                            reason=reason,
                            metadata={
                                "retailer": quarantine_entry["retailer"],
                                "receipt_id": quarantine_entry["receipt_id"],
                                "purchase_date": quarantine_entry["purchase_date"],
                            },
                        )
                    except Exception:
                        pass
                log_event(
                    LOGGER,
                    "receipt_processing_item_auto_quarantined",
                    correlation_id=message_id,
                    vendor_id=vendor_id,
                    reason=reason,
                )
                continue

            log_event(
                LOGGER,
                "receipt_processing_item_requires_review",
                correlation_id=message_id,
                vendor_id=vendor_id,
                reason_code="receipt_processing_error",
                message_id=message_id,
                failure_detail=failure_detail,
            )
            raise ReceiptReviewRequiredError(
                retailer=vendor_id,
                reason_code="receipt_processing_error",
                email_sender=source["email_sender"],
                email_subject=source["email_subject"],
                message_id=message_id,
                failure_detail=failure_detail,
            ) from error

    log_event(
        LOGGER,
        "receipt_processing_completed",
        receipt_count=len(results),
        manual_count=len(collected_manual_receipts),
        status="completed",
    )
    return ProcessedReceipts(results, collected_manual_receipts)



def _cached_source(record: Mapping[str, Any]) -> dict[str, str]:
    source = record.get("source")
    if not isinstance(source, Mapping):
        raise WorkflowError("Cached receipt record has no valid source metadata.")
    required_fields = ("message_id", "vendor_id", "email_sender", "email_subject")
    values = {field: source.get(field) for field in required_fields}
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise WorkflowError("Cached receipt record has invalid source metadata.")
    return {field: values[field] for field in required_fields}


def _cached_receipt(record: Mapping[str, Any]) -> Mapping[str, object]:
    receipt = record.get("receipt")
    if not isinstance(receipt, Mapping):
        raise WorkflowError("Cached receipt record has no parsed receipt.")
    return receipt


def _cached_reason_code(record: Mapping[str, Any]) -> str:
    reason_code = record.get("reason_code")
    if not isinstance(reason_code, str) or not reason_code:
        raise WorkflowError("Cached receipt review record has no reason code.")
    return reason_code


def _resolve_email_payer(
    retailer: str,
    last_four: str | None,
    manual_payer_id: str | None,
    config: Mapping[str, Any],
) -> tuple[str, str]:
    try:
        return resolve_payer(last_four, manual_payer_id, config)
    except ConfigError as error:
        is_unmapped_payment: bool = (
            manual_payer_id is None
            and str(error).startswith("No payer mapping exists for the supplied card")
        )
        if is_unmapped_payment:
            raise UnknownPaymentMappingError(retailer, last_four) from error
        raise


def _vendor_id_for_email(
    email: ReceiptEmail, signatures: Mapping[str, Mapping[str, object]]
) -> str:
    for vendor_id, signature in signatures.items():
        if matches_receipt_signature(email.sender, email.subject, signature):
            return vendor_id
    raise WorkflowError("Receipt email does not match a configured signature.")


def _optional_string(value: object) -> str | None:
    """Return a non-empty parsed field without manufacturing context."""
    return value if isinstance(value, str) and value.strip() else None

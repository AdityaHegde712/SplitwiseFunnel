"""Application workflow for turning validated receipt emails into split results."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from src.app.config import ConfigError, resolve_payer
from src.app.results import ResultError, build_result
from src.domain.allocate import AllocationError, allocate_receipt
from src.gmail.client import ReceiptEmail
from src.parsers.receipts import ReceiptParseError, parse_receipt_email


class WorkflowError(ValueError):
    """Raised when a receipt email cannot safely become an allocation result."""


class UnknownPaymentMappingError(WorkflowError):
    """Raised when a parsed payment card has no local payer mapping."""

    def __init__(self, retailer: str, last_four: str | None) -> None:
        self.retailer: str = retailer
        self.last_four: str | None = last_four
        super().__init__("Receipt payment has no configured payer mapping.")


def process_receipt_emails(
    emails: Sequence[ReceiptEmail],
    config: Mapping[str, Any],
    signatures: Mapping[str, Mapping[str, str]],
    manual_payer_id: str | None = None,
) -> list[dict[str, Any]]:
    """Process allowlisted receipt emails in input order without persisting email bodies."""
    results: list[dict[str, Any]] = []
    for email in emails:
        vendor_id: str = _vendor_id_for_email(email, signatures)
        try:
            receipt: dict[str, object] = parse_receipt_email(vendor_id, email.html)
            payment_last_four: object = receipt.get("payment_last_four")
            last_four: str | None = (
                payment_last_four if isinstance(payment_last_four, str) else None
            )
            payer_id, payer_resolution = _resolve_email_payer(
                vendor_id, last_four, manual_payer_id, config
            )
            allocation: dict[str, Any] = allocate_receipt(receipt, config)
            results.append(build_result(receipt, payer_id, payer_resolution, allocation))
        except UnknownPaymentMappingError:
            raise
        except (AllocationError, ConfigError, ReceiptParseError, ResultError) as error:
            raise WorkflowError("Receipt processing failed safely.") from error
    return results


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
    email: ReceiptEmail, signatures: Mapping[str, Mapping[str, str]]
) -> str:
    for vendor_id, signature in signatures.items():
        sender: object = signature.get("sender")
        subject: object = signature.get("subject")
        is_exact_match: bool = email.sender == sender and email.subject == subject
        if is_exact_match:
            return vendor_id
    raise WorkflowError("Receipt email does not match a configured signature.")

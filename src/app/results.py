"""Auditable result construction and local output rendering."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from src.domain.allocate import _split_cents
from src.observability.logging import log_event



LOGGER = logging.getLogger("splitwise_funnel.app.results")


class ResultError(ValueError):
    """Raised when a receipt result cannot be safely assembled or written."""


def build_result(
    receipt: Mapping[str, Any],
    payer_id: str,
    payer_resolution: str,
    allocation: Mapping[str, Any],
    refund_adjustment_cents: int = 0,
    refund_details: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    refund_shares_cents: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Build a JSON-serializable result with exact per-participant totals."""
    if not isinstance(receipt, Mapping):
        raise ResultError("receipt must be an object.")
    if not isinstance(allocation, Mapping):
        raise ResultError("allocation must be an object.")
    _require_nonempty_string(payer_id, "payer_id")
    _require_nonempty_string(payer_resolution, "payer_resolution")

    receipt_data: dict[str, Any] = dict(receipt)
    allocation_data: dict[str, Any] = dict(allocation)
    final_total_cents: int = _require_integer_cents(
        receipt_data.get("final_total_cents"), "receipt.final_total_cents"
    )
    total_allocated_cents: int = _require_integer_cents(
        allocation_data.get("total_allocated_cents"), "allocation.total_allocated_cents"
    )
    if total_allocated_cents != final_total_cents:
        raise ResultError("Allocation total does not reconcile with receipt final total.")

    participant_totals_cents: dict[str, int] = _aggregate_participant_totals(allocation_data)
    if sum(participant_totals_cents.values()) != final_total_cents:
        raise ResultError("Participant totals do not reconcile with receipt final total.")

    adjustment_cents: int = _require_integer_cents(
        refund_adjustment_cents, "refund_adjustment_cents"
    )
    if adjustment_cents > 0:
        raise ResultError("refund_adjustment_cents must not be positive.")

    if adjustment_cents < 0:
        if refund_shares_cents is not None:
            shares_to_apply = dict(refund_shares_cents)
        elif isinstance(refund_details, Mapping) and "shares_cents" in refund_details:
            shares_to_apply = dict(refund_details["shares_cents"])
        else:
            shares_to_apply = _split_cents(
                adjustment_cents, list(participant_totals_cents.keys())
            )

        if sum(shares_to_apply.values()) != adjustment_cents:
            raise ResultError(
                "Refund shares do not reconcile with refund_adjustment_cents."
            )

        for p_id, p_share in shares_to_apply.items():
            _require_integer_cents(p_share, f"refund share for {p_id}")
            participant_totals_cents[p_id] = (
                participant_totals_cents.get(p_id, 0) + p_share
            )

    net_total_cents: int = final_total_cents + adjustment_cents
    if sum(participant_totals_cents.values()) != net_total_cents:
        raise ResultError(
            "Participant totals do not reconcile with receipt net total."
        )

    result: dict[str, Any] = {
        "receipt": receipt_data,
        "payer": {"participant_id": payer_id, "resolution": payer_resolution},
        "allocation": allocation_data,
        "participant_totals_cents": participant_totals_cents,
        "original_total_cents": final_total_cents,
        "refund_adjustment_cents": adjustment_cents,
        "net_total_cents": net_total_cents,
    }
    if refund_details is not None:
        result["refund_details"] = refund_details

    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        log_event(LOGGER, "result_build_failed", status="not_serializable")
        raise ResultError("Result contains values that cannot be serialized as JSON.") from error
    log_event(
        LOGGER,
        "result_built",
        item_count=len(_require_items(allocation_data)),
        participant_count=len(participant_totals_cents),
        total_cents=final_total_cents,
        net_total_cents=net_total_cents,
        refund_adjustment_cents=adjustment_cents,
        payer_resolution=payer_resolution,
    )
    return result


def aggregate_run_results(
    results: Sequence[Mapping[str, Any]],
    manual_receipts: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Reconcile selected receipt results into household-wide gross totals."""
    participant_totals_cents: dict[str, int] = {}
    total_cents: int = 0
    for index, result in enumerate(results):
        receipt: Mapping[str, Any] = _require_mapping(
            result.get("receipt"), f"results[{index}].receipt"
        )
        receipt_total_cents: int = _require_integer_cents(
            receipt.get("final_total_cents"), f"results[{index}].receipt.final_total_cents"
        )
        expected_receipt_total_cents: int = receipt_total_cents
        if "net_total_cents" in result:
            expected_receipt_total_cents = _require_integer_cents(
                result.get("net_total_cents"), f"results[{index}].net_total_cents"
            )
        receipt_participant_totals: Mapping[str, Any] = _require_mapping(
            result.get("participant_totals_cents"),
            f"results[{index}].participant_totals_cents",
        )
        subtotal_cents: int = 0
        for participant_id, amount_cents in receipt_participant_totals.items():
            _require_nonempty_string(
                participant_id, f"results[{index}] participant id"
            )
            participant_share_cents: int = _require_integer_cents(
                amount_cents, f"results[{index}] participant total"
            )
            participant_totals_cents[participant_id] = (
                participant_totals_cents.get(participant_id, 0) + participant_share_cents
            )
            subtotal_cents += participant_share_cents
        if subtotal_cents != expected_receipt_total_cents:
            raise ResultError("Receipt participant totals do not reconcile with its final total.")
        total_cents += expected_receipt_total_cents

    payer_groups: dict[str, list[Mapping[str, Any]]] = {}
    for result in results:
        payer_info = result.get("payer")
        if isinstance(payer_info, Mapping) and payer_info.get("participant_id"):
            payer_id = str(payer_info["participant_id"])
            payer_groups.setdefault(payer_id, []).append(result)

    payer_aggregates: dict[str, dict[str, Any]] = {}
    for payer_id, payer_results in payer_groups.items():
        payer_total_cents = 0
        payer_receipt_ids: list[str] = []
        payer_participant_shares: dict[str, int] = {}
        for res in payer_results:
            net = res.get("net_total_cents")
            if net is None:
                net = res.get("receipt", {}).get("final_total_cents", 0)
            payer_total_cents += int(net)

            r_id = res.get("receipt", {}).get("receipt_id")
            if r_id:
                payer_receipt_ids.append(str(r_id))

            p_totals = res.get("participant_totals_cents", {})
            if isinstance(p_totals, Mapping):
                for p_id, p_cents in p_totals.items():
                    payer_participant_shares[p_id] = (
                        payer_participant_shares.get(p_id, 0) + int(p_cents)
                    )

        payer_name = _display_id(payer_id)
        p_receipt_count = len(payer_results)
        lines = [
            f"## Paid by {payer_name}",
            "",
            f"- Receipts: {p_receipt_count}",
            f"- Total: {_format_cents(payer_total_cents)}",
            "",
            "### Per-person shares",
            "",
        ]
        for p_id, p_cents in payer_participant_shares.items():
            lines.append(f"- {_display_id(p_id)}: {_format_cents(p_cents)}")
        payer_md = "\n".join(lines) + "\n"

        payer_aggregates[payer_id] = {
            "total_cents": payer_total_cents,
            "receipt_count": p_receipt_count,
            "receipt_ids": payer_receipt_ids,
            "participant_shares_cents": payer_participant_shares,
            "markdown_summary": payer_md,
        }

    if manual_receipts is None:
        manual_receipts = getattr(results, "manual_receipts", [])

    automated_count = len(results)
    manual_count = len(manual_receipts)
    total_count = automated_count + manual_count

    aggregate: dict[str, Any] = {
        "receipt_count": automated_count,
        "automated_count": automated_count,
        "manual_count": manual_count,
        "total_count": total_count,
        "manual_receipts": list(manual_receipts),
        "total_cents": total_cents,
        "participant_totals_cents": participant_totals_cents,
        "payer_aggregates": payer_aggregates,
    }
    log_event(
        LOGGER,
        "run_results_aggregated",
        receipt_count=automated_count,
        automated_count=automated_count,
        manual_count=manual_count,
        total_count=total_count,
        participant_count=len(participant_totals_cents),
        total_cents=total_cents,
    )
    return aggregate


def render_markdown_run_summary(aggregate: Mapping[str, Any]) -> str:
    """Render a copy-ready gross-share summary for one completed receipt run."""
    receipt_count: int = _require_integer_cents(
        aggregate.get("receipt_count"), "aggregate.receipt_count"
    )
    if receipt_count < 0:
        raise ResultError("aggregate.receipt_count must not be negative.")
    total_cents: int = _require_integer_cents(
        aggregate.get("total_cents"), "aggregate.total_cents"
    )
    participant_totals: Mapping[str, Any] = _require_mapping(
        aggregate.get("participant_totals_cents"), "aggregate.participant_totals_cents"
    )
    if sum(
        _require_integer_cents(amount_cents, "aggregate participant total")
        for amount_cents in participant_totals.values()
    ) != total_cents:
        raise ResultError("Aggregate participant totals do not reconcile with the run total.")

    lines: list[str] = [
        "# Run total",
        "",
        f"- Receipts: {receipt_count}",
        f"- Household total: {_format_cents(total_cents)}",
        "",
        "## Per-person totals",
        "",
    ]
    for participant_id, amount_cents in participant_totals.items():
        _require_nonempty_string(participant_id, "aggregate participant id")
        lines.append(
            f"- {_display_id(participant_id)}: "
            f"{_format_cents(_require_integer_cents(amount_cents, 'aggregate participant total'))}"
        )

    payer_aggregates = aggregate.get("payer_aggregates")
    if isinstance(payer_aggregates, Mapping) and payer_aggregates:
        for payer_id, payer_data in payer_aggregates.items():
            if isinstance(payer_data, Mapping) and "markdown_summary" in payer_data:
                lines.append("")
                lines.append(str(payer_data["markdown_summary"]).strip())

    manual_receipts = aggregate.get("manual_receipts")
    if isinstance(manual_receipts, Sequence) and manual_receipts:
        lines.extend([
            "",
            "## Manual Processing Required",
            "",
        ])
        for entry in manual_receipts:
            if isinstance(entry, Mapping):
                r_id = entry.get("receipt_id") or entry.get("message_id") or "Unknown"
                r_date = entry.get("purchase_date") or entry.get("date") or "Unknown"
                r_retailer = _display_id(str(entry.get("retailer") or "Unknown"))
                r_reason = str(entry.get("reason") or "Manual processing required")
                lines.append(
                    f"- ID: {r_id} | Date: {r_date} | Retailer: {r_retailer} | Reason: {r_reason}"
                )

    return "\n".join(lines) + "\n"



def render_markdown_summary(result: Mapping[str, Any]) -> str:
    """Render a copy-ready Markdown receipt summary."""
    receipt: Mapping[str, Any] = _require_mapping(result.get("receipt"), "result.receipt")
    payer: Mapping[str, Any] = _require_mapping(result.get("payer"), "result.payer")
    participant_totals: Mapping[str, Any] = _require_mapping(
        result.get("participant_totals_cents"), "result.participant_totals_cents"
    )
    allocation: Mapping[str, Any] = _require_mapping(result.get("allocation"), "result.allocation")
    receipt_id: str = _require_string_field(receipt, "receipt_id")
    retailer: str = _require_string_field(receipt, "retailer")
    purchase_date: str = _require_string_field(receipt, "purchase_date")
    final_total_cents: int = _require_integer_cents(
        receipt.get("final_total_cents"), "receipt.final_total_cents"
    )
    payer_id: str = _require_string_field(payer, "participant_id")
    refund_adj_cents: int = result.get("refund_adjustment_cents", 0)
    net_total_cents: int = result.get("net_total_cents", final_total_cents + refund_adj_cents)

    lines: list[str] = [
        f"# Receipt {receipt_id.upper()}",
        "",
        f"- Retailer: {_display_id(retailer)}",
        f"- Purchase date: {purchase_date}",
        f"- Paid by: {_display_id(payer_id)}",
    ]
    if refund_adj_cents < 0:
        lines.extend([
            f"- Original total: {_format_cents(final_total_cents)}",
            f"- Refund adjustment: {_format_cents(refund_adj_cents)}",
            f"- Net total: {_format_cents(net_total_cents)}",
        ])
    else:
        lines.append(f"- Receipt total: {_format_cents(final_total_cents)}")

    lines.extend([
        "",
        "## Per-person totals",
        "",
    ])

    for participant_id, total_cents in participant_totals.items():
        total: int = _require_integer_cents(
            total_cents, f"participant total for {participant_id!r}"
        )
        lines.append(f"- {_display_id(participant_id)}: {_format_cents(total)}")

    lines.extend(["", "## Item allocation", ""])
    for index, item in enumerate(_require_items(allocation)):
        item_mapping: Mapping[str, Any] = _require_mapping(item, f"allocation.items[{index}]")
        description: str = _require_string_field(item_mapping, "description")
        shares: Mapping[str, Any] = _require_mapping(
            item_mapping.get("shares_cents"), f"allocation.items[{index}].shares_cents"
        )
        lines.append(f"- {description} — {_render_shares(shares, f'allocation.items[{index}].shares_cents')}")

    residual_shares: Mapping[str, Any] = _require_mapping(
        allocation.get("residual_shares_cents"), "allocation.residual_shares_cents"
    )
    lines.extend(
        [
            "",
            "## Receipt-level residual",
            "",
            f"- {_render_shares(residual_shares, 'allocation.residual_shares_cents')}",
        ]
    )
    return "\n".join(lines) + "\n"


def write_result_files(
    result: Mapping[str, Any], output_directory: Path, timestamp: str
) -> tuple[Path, Path]:
    """Write one JSON audit record and one Markdown summary using LF newlines."""
    if not isinstance(output_directory, Path):
        raise ResultError("output_directory must be a pathlib.Path.")
    _require_nonempty_string(timestamp, "timestamp")
    log_event(LOGGER, "result_write_started")
    output_directory.mkdir(parents=True, exist_ok=True)

    json_path: Path = output_directory / f"receipt_result_{timestamp}.json"
    markdown_path: Path = output_directory / f"receipt_summary_{timestamp}.md"
    serialized_result: str = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    json_path.write_text(serialized_result, encoding="utf-8", newline="\n")
    markdown_path.write_text(render_markdown_summary(result), encoding="utf-8", newline="\n")
    log_event(LOGGER, "result_files_written", file_count=2)
    return json_path, markdown_path


def _aggregate_participant_totals(allocation: Mapping[str, Any]) -> dict[str, int]:
    totals: dict[str, int] = {}
    items: list[Any] = _require_items(allocation)
    for index, item in enumerate(items):
        item_mapping: Mapping[str, Any] = _require_mapping(item, f"allocation.items[{index}]")
        _add_shares(totals, item_mapping.get("shares_cents"), f"allocation.items[{index}]")
    residual_shares: Mapping[str, Any] = _require_mapping(
        allocation.get("residual_shares_cents"), "allocation.residual_shares_cents"
    )
    _add_shares(totals, residual_shares, "allocation.residual_shares_cents")
    return totals


def _require_items(allocation: Mapping[str, Any]) -> list[Any]:
    items: object = allocation.get("items")
    if not isinstance(items, list):
        raise ResultError("allocation.items must be an array.")
    return items


def _render_shares(shares: Mapping[str, Any], field_name: str) -> str:
    rendered_shares: list[str] = []
    for participant_id, cents in shares.items():
        _require_nonempty_string(participant_id, f"{field_name} participant id")
        rendered_shares.append(
            f"{_display_id(participant_id)} "
            f"{_format_cents(_require_integer_cents(cents, f'{field_name} share'))}"
        )
    return ", ".join(rendered_shares)


def _add_shares(totals: dict[str, int], shares: object, field_name: str) -> None:
    share_mapping: Mapping[str, Any] = _require_mapping(shares, f"{field_name}.shares_cents")
    for participant_id, cents in share_mapping.items():
        _require_nonempty_string(participant_id, f"{field_name} participant id")
        totals[participant_id] = totals.get(participant_id, 0) + _require_integer_cents(
            cents, f"{field_name} share"
        )


def _require_mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResultError(f"{field_name} must be an object.")
    return value


def _require_string_field(mapping: Mapping[str, Any], field_name: str) -> str:
    value: object = mapping.get(field_name)
    _require_nonempty_string(value, field_name)
    return value


def _require_nonempty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ResultError(f"{field_name} must be a non-empty string.")


def _require_integer_cents(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResultError(f"{field_name} must be an integer cent value.")
    return value


def _format_cents(cents: int) -> str:
    sign: str = "-" if cents < 0 else ""
    absolute_cents: int = abs(cents)
    return f"{sign}${absolute_cents // 100}.{absolute_cents % 100:02d}"


def _display_id(identifier: str) -> str:
    return identifier.replace("_", " ").replace("-", " ").title()

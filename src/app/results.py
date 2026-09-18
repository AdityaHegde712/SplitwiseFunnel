"""Auditable result construction and local output rendering."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class ResultError(ValueError):
    """Raised when a receipt result cannot be safely assembled or written."""


def build_result(
    receipt: Mapping[str, Any],
    payer_id: str,
    payer_resolution: str,
    allocation: Mapping[str, Any],
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

    result: dict[str, Any] = {
        "receipt": receipt_data,
        "payer": {"participant_id": payer_id, "resolution": payer_resolution},
        "allocation": allocation_data,
        "participant_totals_cents": participant_totals_cents,
    }
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ResultError("Result contains values that cannot be serialized as JSON.") from error
    return result


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

    lines: list[str] = [
        f"# Receipt {receipt_id.upper()}",
        "",
        f"- Retailer: {_display_id(retailer)}",
        f"- Purchase date: {purchase_date}",
        f"- Paid by: {_display_id(payer_id)}",
        f"- Receipt total: {_format_cents(final_total_cents)}",
        "",
        "## Per-person totals",
        "",
    ]
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
    output_directory.mkdir(parents=True, exist_ok=True)

    json_path: Path = output_directory / f"receipt_result_{timestamp}.json"
    markdown_path: Path = output_directory / f"receipt_summary_{timestamp}.md"
    serialized_result: str = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    json_path.write_text(serialized_result, encoding="utf-8", newline="\n")
    markdown_path.write_text(render_markdown_summary(result), encoding="utf-8", newline="\n")
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

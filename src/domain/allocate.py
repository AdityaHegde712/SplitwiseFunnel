"""Deterministic, exact-cent allocation for finalized receipts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
import logging
import re
from typing import Any

from src.observability.logging import log_event


LOGGER = logging.getLogger("splitwise_funnel.domain.allocate")


class AllocationError(ValueError):
    """Raised when a receipt cannot be allocated safely."""


def get_present_participants(
    participant_ids: Sequence[str],
    purchase_date: date | str,
    absences: Sequence[Mapping[str, Any]] | None = None,
) -> list[str]:
    """Return participants who are not marked absent on the given purchase date."""
    parsed_date: date = (
        purchase_date if isinstance(purchase_date, date) else _parse_date(purchase_date)
    )
    absence_list: Sequence[Mapping[str, Any]] = absences or []
    return [
        participant_id
        for participant_id in participant_ids
        if not _is_absent(participant_id, parsed_date, absence_list)
    ]


def allocate_receipt(
    receipt: Mapping[str, Any], config: Mapping[str, Any]
) -> dict[str, Any]:
    """Allocate receipt item amounts and any receipt-level residual exactly."""
    log_event(LOGGER, "allocation_started")
    purchase_date: date = _parse_date(receipt["purchase_date"])
    participant_ids: list[str] = list(config["participant_ids"])
    if not participant_ids:
        raise AllocationError("At least one participant is required.")

    present_participants: list[str] = get_present_participants(
        participant_ids, purchase_date, config.get("absences", [])
    )
    if not present_participants:
        raise AllocationError("No present participants can receive this receipt.")


    items: Sequence[Mapping[str, Any]] = receipt["items"]
    allocated_items: list[dict[str, Any]] = []
    item_total_cents: int = 0
    rules: Sequence[Mapping[str, Any]] = config.get("rules", [])

    for item in items:
        amount_cents: int = _require_integer_cents(item["amount_cents"], "item amount")
        item_total_cents += amount_cents
        eligible_participants: list[str] = _eligible_participants(
            item["description"], present_participants, rules
        )
        if not eligible_participants:
            raise AllocationError(
                f"No eligible participants remain for item: {item['description']!r}."
            )
        allocated_items.append(
            {
                **dict(item),
                "shares_cents": _split_cents(amount_cents, eligible_participants),
            }
        )

    final_total_cents: int = _require_integer_cents(
        receipt["final_total_cents"], "final total"
    )
    residual_cents: int = final_total_cents - item_total_cents
    residual_shares_cents: dict[str, int] = _split_cents(
        residual_cents, present_participants
    )
    total_allocated_cents: int = sum(
        sum(item["shares_cents"].values()) for item in allocated_items
    ) + sum(residual_shares_cents.values())

    allocation: dict[str, Any] = {
        "items": allocated_items,
        "residual_shares_cents": residual_shares_cents,
        "total_allocated_cents": total_allocated_cents,
    }
    log_event(
        LOGGER,
        "allocation_completed",
        item_count=len(allocated_items),
        present_participant_count=len(present_participants),
        total_cents=total_allocated_cents,
        residual_cents=residual_cents,
    )
    return allocation


def _eligible_participants(
    description: object,
    present_participants: Sequence[str],
    rules: Sequence[Mapping[str, Any]],
) -> list[str]:
    normalized_description: str = _normalize_description(description)
    matching_rules: list[Mapping[str, Any]] = [
        rule
        for rule in rules
        if _normalize_description(rule.get("match_description", ""))
        == normalized_description
    ]

    include_only_ids: set[str] | None = None
    excluded_ids: set[str] = set()
    for rule in matching_rules:
        if "include_only" in rule:
            include_only_ids = set(rule["include_only"])
        excluded_ids.update(rule.get("exclude", []))

    base_participants: Sequence[str] = present_participants
    if include_only_ids is not None:
        base_participants = [
            participant_id
            for participant_id in present_participants
            if participant_id in include_only_ids
        ]
    return [
        participant_id
        for participant_id in base_participants
        if participant_id not in excluded_ids
    ]


def _is_absent(
    participant_id: str,
    purchase_date: date,
    absences: Sequence[Mapping[str, Any]],
) -> bool:
    for absence in absences:
        if absence.get("participant_id") != participant_id:
            continue
        starts_on: date = _parse_date(absence["starts_on"])
        ends_on: date = _parse_date(absence["ends_on"])
        if starts_on <= purchase_date <= ends_on:
            return True
    return False


def _split_cents(amount_cents: int, participant_ids: Sequence[str]) -> dict[str, int]:
    if not participant_ids:
        raise AllocationError("Cannot divide cents among zero participants.")

    direction: int = -1 if amount_cents < 0 else 1
    base_share_cents, remainder_cents = divmod(abs(amount_cents), len(participant_ids))
    return {
        participant_id: direction
        * (base_share_cents + (1 if index < remainder_cents else 0))
        for index, participant_id in enumerate(participant_ids)
    }


split_cents = _split_cents



def _normalize_description(description: object) -> str:
    if not isinstance(description, str):
        raise AllocationError("Item descriptions must be strings.")
    return re.sub(r"\s+", " ", description.strip()).casefold()


def _parse_date(value: object) -> date:
    if not isinstance(value, str):
        raise AllocationError("Dates must use ISO YYYY-MM-DD format.")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise AllocationError("Dates must use ISO YYYY-MM-DD format.") from error


def _require_integer_cents(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AllocationError(f"{field_name.capitalize()} must be an integer cent value.")
    return value

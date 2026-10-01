"""Configuration loading and payer resolution for local receipt processing."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from src.observability.logging import log_event


LOGGER = logging.getLogger("splitwise_funnel.app.config")


class ConfigError(ValueError):
    """Raised when local configuration is incomplete or unsafe to use."""


def load_config(config_path: Path) -> dict[str, Any]:
    """Load and validate a UTF-8 JSON configuration file."""
    if not isinstance(config_path, Path):
        raise ConfigError("Configuration path must be a pathlib.Path.")

    log_event(LOGGER, "config_load_started")
    try:
        raw_config: object = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        log_event(LOGGER, "config_load_failed", status="not_found")
        raise ConfigError(f"Configuration file was not found: {config_path}.") from error
    except UnicodeDecodeError as error:
        log_event(LOGGER, "config_load_failed", status="invalid_encoding")
        raise ConfigError(f"Configuration file is not valid UTF-8: {config_path}.") from error
    except json.JSONDecodeError as error:
        log_event(LOGGER, "config_load_failed", status="invalid_json")
        raise ConfigError(f"Configuration file is not valid JSON: {config_path}.") from error

    if not isinstance(raw_config, dict):
        log_event(LOGGER, "config_load_failed", status="invalid_root")
        raise ConfigError("Configuration root must be a JSON object.")

    _validate_config(raw_config)
    log_event(
        LOGGER,
        "config_loaded",
        participant_count=len(raw_config["participant_ids"]),
        payment_mapping_count=len(raw_config.get("payment_mappings", [])),
        absence_count=len(raw_config.get("absences", [])),
        rule_count=len(raw_config.get("rules", [])),
    )
    return raw_config


def append_payment_mapping(
    config_path: Path,
    last_four: str,
    payer_id: str,
) -> dict[str, Any]:
    """Persist a newly resolved payment instrument for an existing payer."""
    config: dict[str, Any] = load_config(config_path)
    participant_ids: list[str] = _validated_participant_ids(config)
    payment_mappings: list[Mapping[str, Any]] = _validated_payment_mappings(
        config, participant_ids
    )
    _validate_last_four(last_four, "last_four")
    _require_nonempty_string(payer_id, "payer_id")
    if payer_id not in participant_ids:
        raise ConfigError(f"Payer {payer_id!r} is not a configured participant.")

    _ensure_mapping_is_new(payment_mappings, last_four)
    config["payment_mappings"].append({"last_four": last_four, "payer_id": payer_id})
    _validate_config(config)
    _write_config_atomically(config_path, config)
    log_event(LOGGER, "payment_mapping_appended", payment_mapping_count=len(config["payment_mappings"]))
    return config


def append_participant(
    config_path: Path,
    participant_id: str,
) -> dict[str, Any]:
    """Persist a new participant without creating a payment mapping."""
    config: dict[str, Any] = load_config(config_path)
    participant_ids: list[str] = _validated_participant_ids(config)
    _require_nonempty_string(participant_id, "participant_id")
    if participant_id in participant_ids:
        raise ConfigError(f"Participant {participant_id!r} is already configured.")

    config["participant_ids"].append(participant_id)
    _validate_config(config)
    _write_config_atomically(config_path, config)
    log_event(LOGGER, "participant_appended", participant_count=len(config["participant_ids"]))
    return config


def append_participant_and_payment_mapping(
    config_path: Path,
    last_four: str,
    participant_id: str,
) -> dict[str, Any]:
    """Persist a new participant and their newly resolved payment instrument."""
    config: dict[str, Any] = load_config(config_path)
    participant_ids: list[str] = _validated_participant_ids(config)
    payment_mappings: list[Mapping[str, Any]] = _validated_payment_mappings(
        config, participant_ids
    )
    _require_nonempty_string(participant_id, "participant_id")
    if participant_id in participant_ids:
        raise ConfigError(f"Participant {participant_id!r} is already configured.")
    _validate_last_four(last_four, "last_four")
    _ensure_mapping_is_new(payment_mappings, last_four)

    config["participant_ids"].append(participant_id)
    config["payment_mappings"].append(
        {"last_four": last_four, "payer_id": participant_id}
    )
    _validate_config(config)
    _write_config_atomically(config_path, config)
    log_event(
        LOGGER,
        "participant_and_payment_mapping_appended",
        participant_count=len(config["participant_ids"]),
        payment_mapping_count=len(config["payment_mappings"]),
    )
    return config


def replace_config(config_path: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and atomically replace the local household configuration."""
    if not isinstance(config, Mapping):
        raise ConfigError("Configuration root must be a JSON object.")
    replacement: dict[str, Any] = dict(config)
    _validate_config(replacement)
    _write_config_atomically(config_path, replacement)
    log_event(
        LOGGER,
        "config_replaced",
        participant_count=len(replacement["participant_ids"]),
        payment_mapping_count=len(replacement.get("payment_mappings", [])),
    )
    return replacement


def resolve_payer(
    payment_last_four: str | None,
    explicit_payer_id: str | None,
    config: Mapping[str, Any],
) -> tuple[str, str]:
    """Resolve a payer from a card mapping, or a validated manual selection."""
    participant_ids: list[str] = _validated_participant_ids(config)
    payment_mappings: list[Mapping[str, Any]] = _validated_payment_mappings(
        config, participant_ids
    )

    if payment_last_four is not None:
        _validate_last_four(payment_last_four)
        for payment_mapping in payment_mappings:
            if payment_mapping["last_four"] == payment_last_four:
                log_event(LOGGER, "payer_resolved", resolution="card_mapping")
                return payment_mapping["payer_id"], "card_last_four"

    if explicit_payer_id is not None:
        if explicit_payer_id not in participant_ids:
            raise ConfigError(
                f"Manual payer {explicit_payer_id!r} is not a configured participant."
            )
        log_event(LOGGER, "payer_resolved", resolution="manual")
        return explicit_payer_id, "manual"

    log_event(LOGGER, "payer_resolution_failed", status="no_mapping")
    raise ConfigError("No payer mapping exists for the supplied card last four. Choose a configured manual payer.")


def _validate_config(config: Mapping[str, Any]) -> None:
    participant_ids: list[str] = _validated_participant_ids(config)
    _validated_payment_mappings(config, participant_ids)


def _ensure_mapping_is_new(
    payment_mappings: list[Mapping[str, Any]], last_four: str
) -> None:
    existing_last_fours: set[str] = {
        payment_mapping["last_four"] for payment_mapping in payment_mappings
    }
    if last_four in existing_last_fours:
        raise ConfigError("A payment mapping already exists for this card last four.")


def _write_config_atomically(config_path: Path, config: Mapping[str, Any]) -> None:
    serialized_config: str = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    temporary_path: Path | None = None
    try:
        temporary_file_descriptor, temporary_file_name = tempfile.mkstemp(
            dir=config_path.parent,
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_file_name)
        with os.fdopen(
            temporary_file_descriptor, "w", encoding="utf-8", newline="\n"
        ) as config_file:
            config_file.write(serialized_config)
        os.replace(temporary_path, config_path)
    except OSError as error:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)
        log_event(LOGGER, "config_write_failed", status="os_error")
        raise ConfigError(f"Unable to persist configuration at {config_path}.") from error


def _validated_participant_ids(config: Mapping[str, Any]) -> list[str]:
    participant_ids: object = config.get("participant_ids")
    if not isinstance(participant_ids, list) or not participant_ids:
        raise ConfigError("participant_ids must be a non-empty JSON array.")
    if any(not isinstance(participant_id, str) or not participant_id.strip() for participant_id in participant_ids):
        raise ConfigError("Each participant_id must be a non-empty string.")
    if len(set(participant_ids)) != len(participant_ids):
        raise ConfigError("participant_ids must not contain duplicates.")
    return participant_ids


def _validated_payment_mappings(
    config: Mapping[str, Any], participant_ids: list[str]
) -> list[Mapping[str, Any]]:
    payment_mappings: object = config.get("payment_mappings", [])
    if not isinstance(payment_mappings, list):
        raise ConfigError("payment_mappings must be a JSON array.")

    seen_last_fours: set[str] = set()
    validated_mappings: list[Mapping[str, Any]] = []
    for index, payment_mapping in enumerate(payment_mappings):
        if not isinstance(payment_mapping, Mapping):
            raise ConfigError(f"payment_mappings[{index}] must be an object.")

        last_four: object = payment_mapping.get("last_four")
        payer_id: object = payment_mapping.get("payer_id")
        _validate_last_four(last_four, f"payment_mappings[{index}].last_four")
        _require_nonempty_string(payer_id, f"payment_mappings[{index}].payer_id")
        if payer_id not in participant_ids:
            raise ConfigError(
                f"payment_mappings[{index}].payer_id {payer_id!r} is not a configured participant."
            )

        if last_four in seen_last_fours:
            raise ConfigError("payment_mappings must not contain duplicate last_four mappings.")
        seen_last_fours.add(last_four)
        validated_mappings.append(payment_mapping)
    return validated_mappings


def _require_nonempty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field_name} must be a non-empty string.")


def _validate_last_four(value: object, field_name: str = "payment_last_four") -> None:
    if not isinstance(value, str) or len(value) != 4 or not value.isdigit():
        raise ConfigError(f"{field_name} must be exactly four digits.")

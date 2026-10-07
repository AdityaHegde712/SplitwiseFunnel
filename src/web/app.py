"""Loopback-only FastAPI adapter for the local Splitwise Funnel UI."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
import json
import os
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request, status
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.responses import Response

from src.app.config import (
    ConfigError,
    append_absence,
    append_participant,
    append_payment_mapping,
    append_receipt_payer_mapping,
    load_config,
    replace_config,
)
from src.app.results import (
    ResultError,
    aggregate_run_results,
    render_markdown_run_summary,
    render_markdown_summary,
    write_result_files,
)
from src.app.receipt_cache import (
    ReceiptCacheError,
    clear_cached_ingestions,
    load_cached_ingestion,
    save_ingestion,
)
from src.app.refund_links import (
    RefundLinkError,
    load_refund_links,
    save_refund_link,
)
from src.app.manual_receipts import (
    DEFAULT_MANUAL_RECEIPTS_PATH,
    ManualReceiptError,
    load_manual_receipts,
    save_manual_receipt,
)
from src.app.workflow import (
    ReceiptRefundLinkRequiredError,
    ReceiptReviewRequiredError,
    UnknownPaymentMappingError,
    WorkflowError,
    ingest_receipt_emails,
    process_cached_receipts,
)
from src.cli import DEFAULT_VENDOR_IDS, RECEIPT_SIGNATURES
from src.gmail.auth import GmailAuthError, OAuthPaths, build_gmail_service
from src.gmail.client import GmailRateLimitError, fetch_receipt_emails
from src.observability.logging import configure_logging, log_event


CORRELATION_ID_HEADER = "X-Correlation-ID"
STATIC_DIRECTORY = Path(__file__).parent / "static"
ALLOWED_HOSTS = ("localhost", "127.0.0.1", "[::1]", "testserver")


class ParticipantRequest(BaseModel):
    """Validated payload for creating a household participant."""

    participant_id: str = Field(min_length=1, max_length=128)


class PaymentMappingRequest(BaseModel):
    """Validated payload for associating a Visa suffix with a payer."""

    last_four: str = Field(pattern=r"^\d{4}$")
    payer_id: str = Field(min_length=1, max_length=128)


class ReceiptRunRequest(BaseModel):
    """Validated selection for one local Gmail receipt run."""

    start_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    vendors: list[str] = Field(min_length=1, max_length=len(DEFAULT_VENDOR_IDS))
    manual_payer_id: str | None = Field(default=None, max_length=128)


class RefundLinkRequest(BaseModel):
    """Validated payload for linking a refund email to an original receipt."""

    refund_id: str = Field(min_length=1, max_length=128)
    original_receipt_id: str = Field(min_length=1, max_length=128)
    refund_amount_cents: int | None = Field(default=None, gt=0)
    refund_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    start_on: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_on: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    vendors: list[str] | None = None
    manual_payer_id: str | None = Field(default=None, max_length=128)


class ManualReceiptRequest(BaseModel):
    """Validated payload for quarantining a receipt for manual processing."""

    message_id: str = Field(min_length=1, max_length=128)
    reason: str = ""
    metadata: dict[str, Any] | None = None
    start_on: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_on: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    vendors: list[str] | None = None
    manual_payer_id: str | None = Field(default=None, max_length=128)



class AbsenceRequest(BaseModel):
    """Validated payload for registering a member absence window."""

    participant_id: str = Field(min_length=1, max_length=128)
    starts_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    ends_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class ReceiptPayerMappingRequest(BaseModel):
    """Validated payload for associating a receipt with a payer."""

    receipt_id: str = Field(min_length=1, max_length=128)
    payer_id: str = Field(min_length=1, max_length=128)


class HouseholdConfigRequest(BaseModel):
    """Validated raw JSON editor payload, checked by the domain config validator."""

    participant_ids: list[str]
    payment_mappings: list[dict[str, Any]] = Field(default_factory=list)
    receipt_payer_mappings: dict[str, str] = Field(default_factory=dict)
    absences: list[dict[str, Any]] = Field(default_factory=list)
    rules: list[dict[str, Any]] = Field(default_factory=list)


def create_app(
    config_path: Path,
    log_directory: Path,
    oauth_directory: Path | None = None,
    refund_links_path: Path | None = None,
    manual_receipts_path: Path | None = None,
) -> FastAPI:
    """Create the local-only web app backed by one household config file."""
    logger = configure_logging(log_directory)
    selected_oauth_directory = oauth_directory or _default_oauth_directory()
    output_directory = config_path.parent / "results"
    ingestion_directory = config_path.parent / "receipt_ingestions"
    selected_refund_links_path = refund_links_path or (config_path.parent / "refund_links.json")
    selected_manual_receipts_path = manual_receipts_path or (config_path.parent / "manual_receipts.json")
    app = FastAPI(title="Splitwise Funnel", docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(ALLOWED_HOSTS))
    app.mount("/static", StaticFiles(directory=STATIC_DIRECTORY), name="static")

    @app.middleware("http")
    async def log_completed_request(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        correlation_id = request.headers.get(CORRELATION_ID_HEADER) or str(uuid4())
        request.state.correlation_id = correlation_id
        started_at = perf_counter()
        response = await call_next(request)
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        log_event(
            logger,
            "http_request_completed",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round((perf_counter() - started_at) * 1000, 3),
            correlation_id=correlation_id,
        )
        return response

    @app.exception_handler(ConfigError)
    async def handle_config_error(request: Request, _: ConfigError) -> JSONResponse:
        correlation_id = _correlation_id(request)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "detail": "Household configuration could not be processed.",
                "correlation_id": correlation_id,
            },
            headers={CORRELATION_ID_HEADER: correlation_id},
        )

    @app.exception_handler(RefundLinkError)
    async def handle_refund_link_error(request: Request, _: RefundLinkError) -> JSONResponse:
        correlation_id = _correlation_id(request)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "detail": "Refund link could not be processed.",
                "correlation_id": correlation_id,
            },
            headers={CORRELATION_ID_HEADER: correlation_id},
        )

    @app.exception_handler(ManualReceiptError)
    async def handle_manual_receipt_error(request: Request, _: ManualReceiptError) -> JSONResponse:
        correlation_id = _correlation_id(request)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "detail": "Manual receipt quarantine entry could not be processed.",
                "correlation_id": correlation_id,
            },
            headers={CORRELATION_ID_HEADER: correlation_id},
        )


    @app.get("/")
    async def serve_ui() -> FileResponse:
        return FileResponse(STATIC_DIRECTORY / "index.html")

    @app.get("/api/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/household")
    async def get_household(request: Request) -> dict[str, Any]:
        config = load_config(config_path)
        return {**config, "correlation_id": _correlation_id(request)}

    @app.post("/api/v1/household/participants", status_code=status.HTTP_201_CREATED)
    async def create_participant(
        request: Request,
        payload: ParticipantRequest,
    ) -> dict[str, Any]:
        config = append_participant(config_path, payload.participant_id.strip())
        return {**config, "correlation_id": _correlation_id(request)}

    @app.post(
        "/api/v1/household/payment-mappings",
        status_code=status.HTTP_201_CREATED,
    )
    async def create_payment_mapping(
        request: Request,
        payload: PaymentMappingRequest,
    ) -> dict[str, Any]:
        config = append_payment_mapping(
            config_path,
            payload.last_four,
            payload.payer_id.strip(),
        )
        return {**config, "correlation_id": _correlation_id(request)}

    @app.post(
        "/api/v1/household/absences",
        status_code=status.HTTP_201_CREATED,
    )
    async def create_absence(
        request: Request,
        payload: AbsenceRequest,
    ) -> dict[str, Any]:
        config = append_absence(
            config_path,
            payload.participant_id.strip(),
            payload.starts_on.strip(),
            payload.ends_on.strip(),
        )
        return {**config, "correlation_id": _correlation_id(request)}

    @app.post(
        "/api/v1/household/receipt-payer-mappings",
        status_code=status.HTTP_201_CREATED,
    )
    async def create_receipt_payer_mapping(
        request: Request,
        payload: ReceiptPayerMappingRequest,
    ) -> dict[str, Any]:
        config = append_receipt_payer_mapping(
            config_path,
            payload.receipt_id.strip(),
            payload.payer_id.strip(),
        )
        return {**config, "correlation_id": _correlation_id(request)}

    @app.put("/api/v1/household")
    async def replace_household(
        request: Request,
        payload: HouseholdConfigRequest,
    ) -> dict[str, Any]:
        config = replace_config(config_path, payload.model_dump())
        return {**config, "correlation_id": _correlation_id(request)}

    @app.post("/api/v1/runs", status_code=status.HTTP_201_CREATED)
    async def run_receipts(
        request: Request,
        payload: ReceiptRunRequest,
    ) -> dict[str, Any]:
        _validate_date_range(payload.start_on, payload.end_on)
        selected_vendors = _validated_vendor_ids(payload.vendors)
        correlation_id = _correlation_id(request)
        log_event(
            logger,
            "receipt_run_started",
            correlation_id=correlation_id,
            vendor_count=len(selected_vendors),
        )
        try:
            records = load_cached_ingestion(
                ingestion_directory,
                payload.start_on,
                payload.end_on,
                selected_vendors,
            )
            cache_source = "local_cache"
            if records is None:
                service = build_gmail_service(
                    OAuthPaths.from_directory(selected_oauth_directory)
                )
                emails = fetch_receipt_emails(
                    service=service,
                    vendor_ids=selected_vendors,
                    starts_on=payload.start_on,
                    ends_on=payload.end_on,
                    signatures=RECEIPT_SIGNATURES,
                )
                if not emails:
                    raise WorkflowError("No matching receipt emails were found.")
                records = ingest_receipt_emails(emails, RECEIPT_SIGNATURES)
                save_ingestion(
                    ingestion_directory,
                    payload.start_on,
                    payload.end_on,
                    selected_vendors,
                    records,
                )
                cache_source = "gmail"
            results = process_cached_receipts(
                records,
                load_config(config_path),
                manual_payer_id=payload.manual_payer_id,
                refund_links_path=selected_refund_links_path,
                manual_receipts_path=selected_manual_receipts_path,
                auto_quarantine=True,
            )
            manual_receipts = getattr(results, "manual_receipts", [])
            summaries = [render_markdown_summary(result) for result in results]
            aggregate = aggregate_run_results(results, manual_receipts=manual_receipts)
            aggregate_summary = render_markdown_run_summary(aggregate)
            _write_run_results(results, output_directory)
        except ReceiptRefundLinkRequiredError as error:
            log_event(
                logger,
                "receipt_run_needs_refund_link",
                correlation_id=correlation_id,
                refund_id=error.refund.get("refund_id"),
            )
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "detail": "A refund needs an original receipt selection before this run can finish.",
                    "reason_code": "refund_receipt_link_required",
                    "refund": error.refund,
                    "candidates": error.candidates,
                    "receipt_source": cache_source,
                    "correlation_id": correlation_id,
                },
                headers={CORRELATION_ID_HEADER: correlation_id},
            )
        except UnknownPaymentMappingError as error:
            log_event(
                logger,
                "receipt_run_needs_payment_mapping",
                correlation_id=correlation_id,
                vendor_id=error.retailer,
                payment_last_four=error.last_four,
            )

            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "detail": "A receipt needs a local payer mapping before it can be split.",
                    "retailer": error.retailer,
                    "last_four": error.last_four,
                    "message_id": error.message_id,
                    "receipt_id": error.receipt_id,
                    "purchase_date": error.purchase_date,
                    "payment_method": error.payment_method,
                    "amount_cents": getattr(error, "amount_cents", None),
                    "email_sender": error.email_sender,
                    "email_subject": error.email_subject,
                    "participants": load_config(config_path)["participant_ids"],
                    "receipt_source": cache_source,
                    "correlation_id": correlation_id,
                },
                headers={CORRELATION_ID_HEADER: correlation_id},
            )
        except ReceiptReviewRequiredError as error:
            log_event(
                logger,
                "receipt_run_requires_review",
                correlation_id=correlation_id,
                vendor_id=error.retailer,
                reason_code=error.reason_code,
                message_id=error.message_id,
                failure_detail=error.failure_detail,
            )
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={
                    "detail": "A separate receipt needs review before this run can finish.",
                    "retailer": error.retailer,
                    "reason_code": error.reason_code,
                    "message_id": error.message_id,
                    "failure_detail": error.failure_detail,
                    "email_sender": error.email_sender,
                    "email_subject": error.email_subject,
                    "receipt_source": cache_source,
                    "correlation_id": correlation_id,
                },
                headers={CORRELATION_ID_HEADER: correlation_id},
            )
        except GmailRateLimitError:
            log_event(
                logger,
                "receipt_run_rate_limited",
                correlation_id=correlation_id,
            )
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "detail": "Gmail temporarily rate limited receipt ingestion. Wait before retrying; no local cache was created for this selection.",
                    "correlation_id": correlation_id,
                },
                headers={CORRELATION_ID_HEADER: correlation_id},
            )
        except GmailAuthError:
            (selected_oauth_directory / "token.json").unlink(missing_ok=True)
            log_event(
                logger,
                "receipt_run_gmail_authorization_failed",
                correlation_id=correlation_id,
            )
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={
                    "detail": "The saved Gmail authorization token could not be refreshed and was removed from token.json. Retry to sign in via your browser; no receipt emails were fetched.",
                    "reason_code": "gmail_authorization_failed",
                    "correlation_id": correlation_id,
                },
                headers={CORRELATION_ID_HEADER: correlation_id},
            )
        except (
            ConfigError,
            ReceiptCacheError,
            ResultError,
            ValueError,
            WorkflowError,
            OSError,
        ) as error:
            log_event(
                logger,
                "receipt_run_failed",
                correlation_id=correlation_id,
                error_type=type(error).__name__,
            )
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={
                    "detail": "Receipt processing could not complete. Verify the dates, local setup, mappings, and supported receipt format.",
                    "correlation_id": correlation_id,
                },
                headers={CORRELATION_ID_HEADER: correlation_id},
            )

        log_event(
            logger,
            "receipt_run_completed",
            correlation_id=correlation_id,
            receipt_count=len(results),
        )
        return {
            "receipt_count": len(results),
            "automated_count": aggregate.get("automated_count", len(results)),
            "manual_count": aggregate.get("manual_count", len(manual_receipts)),
            "total_count": aggregate.get("total_count", len(results) + len(manual_receipts)),
            "manual_receipts": manual_receipts,
            "summaries": summaries,
            "aggregate_totals_cents": aggregate["participant_totals_cents"],
            "aggregate_total_cents": aggregate["total_cents"],
            "aggregate_summary": aggregate_summary,
            "payer_aggregates": aggregate.get("payer_aggregates", {}),
            "receipt_source": cache_source,
            "correlation_id": correlation_id,
        }

    @app.get("/api/v1/runs")
    async def list_runs(request: Request) -> dict[str, Any]:
        return {
            "runs": _load_past_runs(output_directory, logger),
            "correlation_id": _correlation_id(request),
        }

    @app.post("/api/v1/refund-links", status_code=status.HTTP_201_CREATED)
    async def link_refund(
        request: Request,
        payload: RefundLinkRequest,
    ) -> Any:
        correlation_id = _correlation_id(request)
        saved_entry = save_refund_link(
            selected_refund_links_path,
            refund_id=payload.refund_id,
            original_receipt_id=payload.original_receipt_id,
            refund_amount_cents=payload.refund_amount_cents,
            refund_date=payload.refund_date,
        )
        if payload.start_on and payload.end_on and payload.vendors:
            run_payload = ReceiptRunRequest(
                start_on=payload.start_on,
                end_on=payload.end_on,
                vendors=payload.vendors,
                manual_payer_id=payload.manual_payer_id,
            )
            return await run_receipts(request, run_payload)

        return {
            "status": "linked",
            "refund_link": saved_entry,
            "correlation_id": correlation_id,
        }

    @app.get("/api/v1/refund-links")
    async def list_refund_links(request: Request) -> dict[str, Any]:
        links = load_refund_links(selected_refund_links_path)
        return {
            "refund_links": links,
            "correlation_id": _correlation_id(request),
        }

    @app.post("/api/v1/manual-receipts", status_code=status.HTTP_201_CREATED)
    async def add_manual_receipt(
        request: Request,
        payload: ManualReceiptRequest,
    ) -> Any:
        correlation_id = _correlation_id(request)
        saved_entry = save_manual_receipt(
            selected_manual_receipts_path,
            message_id=payload.message_id,
            reason=payload.reason,
            metadata=payload.metadata,
        )
        if payload.start_on and payload.end_on and payload.vendors:
            run_payload = ReceiptRunRequest(
                start_on=payload.start_on,
                end_on=payload.end_on,
                vendors=payload.vendors,
                manual_payer_id=payload.manual_payer_id,
            )
            return await run_receipts(request, run_payload)

        return {
            "status": "quarantined",
            "manual_receipt": saved_entry,
            "correlation_id": correlation_id,
        }

    @app.get("/api/v1/manual-receipts")
    async def list_manual_receipts(request: Request) -> dict[str, Any]:
        receipts = load_manual_receipts(selected_manual_receipts_path)
        return {
            "manual_receipts": receipts,
            "correlation_id": _correlation_id(request),
        }

    @app.post("/api/v1/cache/clear")
    async def clear_cache(request: Request) -> dict[str, Any]:
        cleared_count = clear_cached_ingestions(
            ingestion_directory,
            manual_receipts_path=selected_manual_receipts_path,
        )
        correlation_id = _correlation_id(request)
        return {"cleared_count": cleared_count, "correlation_id": correlation_id}

    @app.post("/api/v1/oauth/reset")
    async def reset_oauth(request: Request) -> dict[str, Any]:
        (selected_oauth_directory / "token.json").unlink(missing_ok=True)
        return {"status": "ok", "cleared": True, "correlation_id": _correlation_id(request)}

    return app



def _correlation_id(request: Request) -> str:
    """Retrieve middleware state without exposing an untrusted header directly."""
    correlation_id: object = getattr(request.state, "correlation_id", None)
    if isinstance(correlation_id, str) and correlation_id:
        return correlation_id
    return str(uuid4())


def _default_oauth_directory() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "SplitwiseFunnel" / "oauth"
    return Path.home() / "AppData" / "Local" / "SplitwiseFunnel" / "oauth"


def _validate_date_range(start_on: str, end_on: str) -> None:
    try:
        start_date = date.fromisoformat(start_on)
        end_date = date.fromisoformat(end_on)
    except ValueError as error:
        raise ValueError("Dates must use YYYY-MM-DD.") from error
    if start_date > end_date:
        raise ValueError("Start date must not be after end date.")


def _validated_vendor_ids(vendor_ids: list[str]) -> list[str]:
    if len(set(vendor_ids)) != len(vendor_ids):
        raise ValueError("Each vendor may be selected only once.")
    if any(vendor_id not in DEFAULT_VENDOR_IDS for vendor_id in vendor_ids):
        raise ValueError("An unsupported vendor was selected.")
    return vendor_ids


def _write_run_results(results: list[dict[str, Any]], output_directory: Path) -> None:
    run_timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for index, result in enumerate(results, start=1):
        write_result_files(result, output_directory, f"{run_timestamp}_{index:03d}_{uuid4().hex[:8]}")


def _load_past_runs(output_directory: Path, logger: Any) -> list[dict[str, Any]]:
    if not output_directory.is_dir():
        return []
    runs: list[dict[str, Any]] = []
    for result_path in sorted(output_directory.glob("receipt_result_*.json"), reverse=True):
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            receipt = result["receipt"]
            payer = result["payer"]
            participant_totals = result["participant_totals_cents"]
            if not isinstance(receipt, dict) or not isinstance(payer, dict) or not isinstance(participant_totals, dict):
                raise ValueError("Invalid persisted result shape.")
            runs.append(
                {
                    "receipt_id": receipt["receipt_id"],
                    "retailer": receipt["retailer"],
                    "purchase_date": receipt["purchase_date"],
                    "final_total_cents": receipt["final_total_cents"],
                    "payer_id": payer["participant_id"],
                    "participant_totals_cents": participant_totals,
                }
            )
        except (KeyError, OSError, ValueError, json.JSONDecodeError):
            log_event(logger, "past_run_skipped", status="invalid_persisted_result")
    return runs

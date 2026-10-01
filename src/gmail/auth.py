"""Local OAuth authorization for the Gmail receipt reader."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any

from src.observability.logging import log_event


LOGGER = logging.getLogger("splitwise_funnel.gmail.auth")


GMAIL_READONLY_SCOPE: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.readonly",
)


class GmailAuthError(RuntimeError):
    """Raised when local Gmail authorization cannot be completed safely."""


@dataclass(frozen=True)
class OAuthPaths:
    """Owner-controlled locations for the OAuth client and refresh token."""

    credentials_path: Path
    token_path: Path

    @classmethod
    def from_directory(cls, directory: Path) -> OAuthPaths:
        """Create the standard OAuth file locations below ``directory``."""
        return cls(
            credentials_path=directory / "credentials.json",
            token_path=directory / "token.json",
        )


def build_gmail_service(paths: OAuthPaths) -> Any:
    """Authorize the local app and return an authenticated Gmail v1 service."""
    if not paths.credentials_path.is_file():
        log_event(LOGGER, "gmail_authorization_failed", status="credentials_missing")
        raise GmailAuthError(
            f"OAuth client file is required at {paths.credentials_path.name!r}: "
            "credentials.json."
        )

    log_event(LOGGER, "gmail_authorization_started")
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as error:
        log_event(LOGGER, "gmail_authorization_failed", status="dependencies_unavailable")
        raise GmailAuthError(
            "Gmail authorization dependencies are unavailable. Install requirements.txt."
        ) from error

    credentials: Any = _load_or_authorize_credentials(
        paths,
        credentials_type=Credentials,
        request_type=Request,
        flow_type=InstalledAppFlow,
    )
    try:
        service: Any = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    except Exception as error:
        log_event(LOGGER, "gmail_service_initialization_failed", status="service_unavailable")
        raise GmailAuthError("Could not initialize the Gmail v1 service.") from error
    log_event(LOGGER, "gmail_service_initialized", status="ready")
    return service


def _load_or_authorize_credentials(
    paths: OAuthPaths,
    credentials_type: Any,
    request_type: Any,
    flow_type: Any,
) -> Any:
    credentials: Any | None = None
    if paths.token_path.is_file():
        try:
            credentials = credentials_type.from_authorized_user_file(
                str(paths.token_path), GMAIL_READONLY_SCOPE
            )
        except Exception as error:
            log_event(LOGGER, "gmail_authorization_failed", status="saved_token_invalid")
            raise GmailAuthError("The saved Gmail authorization token is invalid.") from error

    if credentials is not None and credentials.valid:
        log_event(LOGGER, "gmail_authorization_completed", status="saved_token_valid")
        return credentials

    if credentials is not None and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(request_type())
        except Exception as error:
            log_event(LOGGER, "gmail_authorization_failed", status="token_refresh_failed")
            raise GmailAuthError("The saved Gmail authorization token could not be refreshed.") from error
    else:
        try:
            flow: Any = flow_type.from_client_secrets_file(
                str(paths.credentials_path), GMAIL_READONLY_SCOPE
            )
            credentials = flow.run_local_server(port=0)
        except Exception as error:
            log_event(LOGGER, "gmail_authorization_failed", status="interactive_authorization_failed")
            raise GmailAuthError("Gmail authorization could not be completed.") from error

    if credentials is None or not credentials.valid:
        log_event(LOGGER, "gmail_authorization_failed", status="credentials_invalid")
        raise GmailAuthError("Gmail authorization did not return valid credentials.")

    try:
        paths.token_path.parent.mkdir(parents=True, exist_ok=True)
        paths.token_path.write_text(credentials.to_json(), encoding="utf-8")
    except OSError as error:
        log_event(LOGGER, "gmail_authorization_failed", status="token_save_failed")
        raise GmailAuthError("The Gmail authorization token could not be saved locally.") from error
    log_event(LOGGER, "gmail_authorization_completed", status="authorized")
    return credentials

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock

from src.gmail.auth import (
    GmailAuthError,
    OAuthPaths,
    _load_or_authorize_credentials,
    build_gmail_service,
)


class GmailAuthContractTests(unittest.TestCase):
    def test_refuses_to_start_authorization_without_owner_client_file(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            paths = OAuthPaths.from_directory(Path(temporary_directory))

            with self.assertRaisesRegex(GmailAuthError, "credentials.json"):
                build_gmail_service(paths)

    def test_keeps_client_and_token_files_together_outside_the_repository(self) -> None:
        directory = Path("C:/owner-controlled/oauth")

        paths = OAuthPaths.from_directory(directory)

        self.assertEqual(paths.credentials_path, directory / "credentials.json")
        self.assertEqual(paths.token_path, directory / "token.json")

    def test_unlinks_stale_token_when_credentials_refresh_fails(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            paths = OAuthPaths.from_directory(Path(temporary_directory))
            paths.credentials_path.write_text("{}", encoding="utf-8")
            paths.token_path.write_text("{}", encoding="utf-8")

            mock_creds = MagicMock()
            mock_creds.valid = False
            mock_creds.expired = True
            mock_creds.refresh_token = "dummy_refresh_token"
            mock_creds.refresh.side_effect = RuntimeError("Token revoked")

            mock_creds_type = MagicMock()
            mock_creds_type.from_authorized_user_file.return_value = mock_creds

            with self.assertRaises(GmailAuthError) as context:
                _load_or_authorize_credentials(
                    paths,
                    credentials_type=mock_creds_type,
                    request_type=MagicMock(),
                    flow_type=MagicMock(),
                )

            self.assertFalse(paths.token_path.exists())
            self.assertIn("was removed from token.json", str(context.exception))

    def test_unlinks_invalid_token_when_loading_authorized_user_file_fails(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            paths = OAuthPaths.from_directory(Path(temporary_directory))
            paths.credentials_path.write_text("{}", encoding="utf-8")
            paths.token_path.write_text("corrupted", encoding="utf-8")

            mock_creds_type = MagicMock()
            mock_creds_type.from_authorized_user_file.side_effect = ValueError("Corrupted token")

            with self.assertRaises(GmailAuthError) as context:
                _load_or_authorize_credentials(
                    paths,
                    credentials_type=mock_creds_type,
                    request_type=MagicMock(),
                    flow_type=MagicMock(),
                )

            self.assertFalse(paths.token_path.exists())
            self.assertIn("was removed from token.json", str(context.exception))


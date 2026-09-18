from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.gmail.auth import GmailAuthError, OAuthPaths, build_gmail_service


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

import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from video_lyrics_creator.alignment import read_lyrics
from video_lyrics_creator.envfile import load_env_file, update_env_file
from video_lyrics_creator.google_drive import (
    AUTHORIZATION_ENDPOINT,
    DOCS_DOCUMENT_ENDPOINT,
    DRIVE_READONLY_SCOPE,
    TOKEN_ENDPOINT,
    _authorization_url,
    authorize_google_drive,
    export_gdoc_text,
    parse_gdoc,
)


class _Response:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return self.body


class GoogleDriveTests(unittest.TestCase):
    def test_authorization_saves_refresh_token_without_printing_it(self):
        class FakeServer:
            server_port = 54321
            timeout = None

            def __init__(self, *args, **kwargs):
                self.oauth_result = None

            def handle_request(self):
                self.oauth_result = {"code": "authorization-code", "error": ""}

            def server_close(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "GOOGLE_DRIVE_CLIENT_ID=client-id\n"
                "GOOGLE_DRIVE_CLIENT_SECRET=client-secret\n"
                "GOOGLE_DRIVE_REFRESH_TOKEN=\n",
                encoding="utf-8",
            )
            messages = []
            with patch.dict(os.environ, {}, clear=True), patch(
                "video_lyrics_creator.google_drive.HTTPServer", FakeServer
            ), patch(
                "video_lyrics_creator.google_drive.webbrowser.open", return_value=True
            ), patch(
                "video_lyrics_creator.google_drive._post_form",
                return_value={"access_token": "access-token", "refresh_token": "private-refresh"},
            ):
                result = authorize_google_drive(env_file, output=messages.append)

            values = env_file.read_text(encoding="utf-8")

        self.assertTrue(result["refresh_token_saved"])
        self.assertIn("GOOGLE_DRIVE_REFRESH_TOKEN=private-refresh", values)
        self.assertNotIn("private-refresh", "\n".join(messages))

    def test_gdoc_pointer_parses_document_and_resource_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "song.gdoc"
            source.write_text(
                json.dumps({"doc_id": "abc_DEF-123", "resource_key": "resource-456"}),
                encoding="utf-8",
            )
            self.assertEqual(parse_gdoc(source), ("abc_DEF-123", "resource-456"))

    def test_authorization_url_uses_loopback_pkce_and_readonly_scope(self):
        url = _authorization_url(
            "client-id", "http://127.0.0.1:54321/oauth2callback", "state", "challenge"
        )
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", AUTHORIZATION_ENDPOINT)
        self.assertEqual(query["scope"], [DRIVE_READONLY_SCOPE])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["prompt"], ["consent"])
        self.assertEqual(query["redirect_uri"], ["http://127.0.0.1:54321/oauth2callback"])

    def test_private_gdoc_omits_tab_name_title_and_other_tabs(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "song.gdoc"
            source.write_text(
                json.dumps({"doc_id": "document-123", "resource_key": "resource-456"}),
                encoding="utf-8",
            )
            calls = []

            def fake_urlopen(request, timeout):
                calls.append(request)
                if request.full_url == TOKEN_ENDPOINT:
                    form = urllib.parse.parse_qs(request.data.decode("utf-8"))
                    self.assertEqual(form["grant_type"], ["refresh_token"])
                    return _Response(json.dumps({"access_token": "temporary-access"}).encode())
                expected_url = DOCS_DOCUMENT_ENDPOINT.format(document_id="document-123")
                self.assertEqual(
                    request.full_url,
                    f"{expected_url}?includeTabsContent=true",
                )
                self.assertEqual(request.get_header("Authorization"), "Bearer temporary-access")
                self.assertEqual(
                    request.get_header("X-goog-drive-resource-keys"),
                    "document-123/resource-456",
                )
                return _Response(
                    json.dumps(
                        {
                            "title": "Song document",
                            "tabs": [
                                {
                                    "tabProperties": {"tabId": "main", "title": "Main"},
                                    "documentTab": {
                                        "body": {
                                            "content": [
                                                {
                                                    "paragraph": {
                                                        "elements": [
                                                            {"textRun": {"content": "Main\n"}}
                                                        ]
                                                    }
                                                },
                                                {
                                                    "paragraph": {
                                                        "elements": [
                                                            {
                                                                "textRun": {
                                                                    "content": "Song Title\n"
                                                                }
                                                            }
                                                        ]
                                                    }
                                                },
                                                {
                                                    "paragraph": {
                                                        "elements": [
                                                            {
                                                                "textRun": {
                                                                    "content": "First line\n"
                                                                }
                                                            }
                                                        ]
                                                    }
                                                },
                                                {
                                                    "paragraph": {
                                                        "elements": [
                                                            {
                                                                "textRun": {
                                                                    "content": "Second line\n"
                                                                }
                                                            }
                                                        ]
                                                    }
                                                },
                                            ]
                                        }
                                    },
                                },
                                {
                                    "tabProperties": {"tabId": "notes", "title": "Notes"},
                                    "documentTab": {
                                        "body": {
                                            "content": [
                                                {
                                                    "paragraph": {
                                                        "elements": [
                                                            {
                                                                "textRun": {
                                                                    "content": "Do not use this\n"
                                                                }
                                                            }
                                                        ]
                                                    }
                                                }
                                            ]
                                        }
                                    },
                                },
                            ],
                        }
                    ).encode()
                )

            environment = {
                "GOOGLE_DRIVE_CLIENT_ID": "client-id",
                "GOOGLE_DRIVE_CLIENT_SECRET": "client-secret",
                "GOOGLE_DRIVE_REFRESH_TOKEN": "refresh-token",
            }
            with patch.dict(os.environ, environment, clear=False), patch(
                "video_lyrics_creator.google_drive.urllib.request.urlopen",
                side_effect=fake_urlopen,
            ):
                text = export_gdoc_text(source, env_dir=base)

        self.assertEqual(text, "First line\nSecond line\n")
        self.assertEqual(len(calls), 2)

    def test_read_lyrics_uses_google_export_as_canonical_text(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "song.gdoc"
            source.write_text(json.dumps({"doc_id": "document-123"}), encoding="utf-8")
            with patch(
                "video_lyrics_creator.google_drive.export_gdoc_text",
                return_value="First exact line\n\nSecond exact line\n",
            ):
                lines = read_lyrics(source, env_dir=directory)
        self.assertEqual(lines, ["First exact line", "Second exact line"])

    def test_read_lyrics_removes_square_bracket_annotations_from_gdoc(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "song.gdoc"
            source.write_text(json.dumps({"doc_id": "document-123"}), encoding="utf-8")
            with patch(
                "video_lyrics_creator.google_drive.export_gdoc_text",
                return_value=(
                    "[Verse 1]\n"
                    "Amazing [quietly] grace\n"
                    "How sweet the sound [repeat]\n"
                    "Mercy [echo], flowing down\n"
                    "[]\n"
                    "________\n"
                    "____ [separator] ____\n"
                    "Brackets remain unmatched [on purpose\n"
                ),
            ):
                lines = read_lyrics(source, env_dir=directory)
        self.assertEqual(
            lines,
            [
                "Amazing grace",
                "How sweet the sound",
                "Mercy, flowing down",
                "Brackets remain unmatched [on purpose",
            ],
        )

    def test_plain_text_lyrics_remove_annotations_and_separators(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "song.txt"
            source.write_text(
                "\n"
                "[Verse 1]\n"
                "Line [remove this] one\n"
                "________\n"
                "____ [separator] ____\n"
                "word_with_underscore\n",
                encoding="utf-8",
            )
            lines = read_lyrics(source, env_dir=directory)
        self.assertEqual(lines, ["Line one", "word_with_underscore"])

    def test_refresh_token_update_preserves_other_env_values(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "# credentials\nOPENAI_API_KEY=openai-value\nGOOGLE_DRIVE_REFRESH_TOKEN=old\n",
                encoding="utf-8",
            )
            update_env_file(env_file, {"GOOGLE_DRIVE_REFRESH_TOKEN": "new-token"})
            with patch.dict(os.environ, {}, clear=True):
                load_env_file(env_file)
                self.assertEqual(os.environ["OPENAI_API_KEY"], "openai-value")
                self.assertEqual(os.environ["GOOGLE_DRIVE_REFRESH_TOKEN"], "new-token")


if __name__ == "__main__":
    unittest.main()

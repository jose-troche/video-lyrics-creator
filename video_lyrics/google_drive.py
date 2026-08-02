"""Minimal Google Drive OAuth + document export.

Only two things are needed: turn a client id/secret into a long-lived refresh
token (one interactive login), and export a Google Doc as plain text.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import requests

from .util import VideoLyricsError, log

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
EXPORT_URI = "https://www.googleapis.com/drive/v3/files/{file_id}/export"

ENV_CLIENT_ID = "GOOGLE_DRIVE_CLIENT_ID"
ENV_CLIENT_SECRET = "GOOGLE_DRIVE_CLIENT_SECRET"
ENV_REFRESH_TOKEN = "GOOGLE_DRIVE_REFRESH_TOKEN"


def doc_id_from_gdoc(path: Path) -> str:
    """Read the Drive-for-desktop .gdoc stub and pull out the document id."""
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    try:
        stub = json.loads(raw)
        if isinstance(stub, dict):
            for key in ("doc_id", "resource_id", "id"):
                if stub.get(key):
                    return str(stub[key]).split(":")[-1]
            url = stub.get("url", "")
            found = _doc_id_from_url(url)
            if found:
                return found
    except json.JSONDecodeError:
        pass
    found = _doc_id_from_url(raw)
    if found:
        return found
    raise VideoLyricsError(f"Could not find a Google document id inside {path}")


def _doc_id_from_url(url: str) -> str | None:
    for pattern in (r"/document/d/([A-Za-z0-9_-]{20,})", r"[?&]id=([A-Za-z0-9_-]{20,})"):
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def _credentials() -> tuple[str, str]:
    client_id = os.environ.get(ENV_CLIENT_ID, "").strip()
    client_secret = os.environ.get(ENV_CLIENT_SECRET, "").strip()
    if not client_id or not client_secret:
        raise VideoLyricsError(
            f"Set {ENV_CLIENT_ID} and {ENV_CLIENT_SECRET} in .env "
            "(Google Cloud console → Credentials → OAuth client ID → Desktop app)."
        )
    return client_id, client_secret


def access_token(refresh_token: str | None = None) -> str:
    """Exchange the stored refresh token for a short-lived access token."""
    client_id, client_secret = _credentials()
    refresh_token = refresh_token or os.environ.get(ENV_REFRESH_TOKEN, "").strip()
    if not refresh_token:
        raise VideoLyricsError(
            f"No {ENV_REFRESH_TOKEN} available. Run `video-lyrics google-auth` once."
        )
    response = requests.post(
        TOKEN_URI,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise VideoLyricsError(f"Google token refresh failed: {response.text.strip()}")
    return response.json()["access_token"]


def export_document(doc_id: str, *, mime_type: str = "text/plain") -> str:
    """Download a Google Doc as text."""
    token = access_token()
    response = requests.get(
        EXPORT_URI.format(file_id=doc_id),
        params={"mimeType": mime_type},
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    if response.status_code != 200:
        raise VideoLyricsError(
            f"Google Doc export failed ({response.status_code}): {response.text.strip()[:300]}"
        )
    # Drive exports UTF-8, but the response carries no charset so requests would
    # otherwise fall back to latin-1 and mangle every curly apostrophe.
    return response.content.decode("utf-8-sig", errors="replace")


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        query = urllib.parse.urlparse(self.path).query
        params = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        type(self).result.update(params)
        body = (
            "<html><body style='font-family:sans-serif;padding:3rem'>"
            "<h2>Video Lyrics Creator</h2><p>Google authorization received. "
            "You can close this tab.</p></body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:  # silence the default stderr logging
        return


def interactive_login(*, open_browser: bool = True, timeout: float = 300.0) -> str:
    """Run the loopback OAuth flow and return a refresh token."""
    client_id, client_secret = _credentials()

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    state = secrets.token_urlsafe(16)

    handler = type("Handler", (_CallbackHandler,), {"result": {}})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/"

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{AUTH_URI}?{urllib.parse.urlencode(params)}"

    print("Open this URL to authorize Google Drive access:\n")
    print(f"  {url}\n")
    if open_browser:
        webbrowser.open(url)

    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout)
    server.server_close()

    result = handler.result
    if result.get("error"):
        raise VideoLyricsError(f"Authorization denied: {result['error']}")
    code = result.get("code")
    if not code:
        raise VideoLyricsError("Timed out waiting for the Google authorization redirect.")
    if result.get("state") != state:
        raise VideoLyricsError("OAuth state mismatch; aborting.")

    response = requests.post(
        TOKEN_URI,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise VideoLyricsError(f"Token exchange failed: {response.text.strip()}")
    payload = response.json()
    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        raise VideoLyricsError(
            "Google returned no refresh token. Revoke the app's access and retry."
        )
    log.info("Google authorization complete.")
    return refresh_token


def store_refresh_token(env_path: Path, refresh_token: str) -> None:
    """Write (or replace) GOOGLE_DRIVE_REFRESH_TOKEN in the .env file."""
    env_path = Path(env_path)
    line = f"{ENV_REFRESH_TOKEN}={refresh_token}"
    if env_path.is_file():
        lines = env_path.read_text(encoding="utf-8").splitlines()
        for index, existing in enumerate(lines):
            if existing.strip().startswith(f"{ENV_REFRESH_TOKEN}="):
                lines[index] = line
                break
        else:
            lines.append(line)
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        env_path.write_text(line + "\n", encoding="utf-8")
    os.environ[ENV_REFRESH_TOKEN] = refresh_token

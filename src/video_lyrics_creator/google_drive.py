from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable

from .envfile import load_env_file, load_project_env, update_env_file
from .errors import VideoLyricsError

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
DOCS_DOCUMENT_ENDPOINT = "https://docs.googleapis.com/v1/documents/{document_id}"
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

CLIENT_ID_ENV = "GOOGLE_DRIVE_CLIENT_ID"
CLIENT_SECRET_ENV = "GOOGLE_DRIVE_CLIENT_SECRET"
REFRESH_TOKEN_ENV = "GOOGLE_DRIVE_REFRESH_TOKEN"


def parse_gdoc(path: str | Path) -> tuple[str, str]:
    source = Path(path).expanduser().resolve()
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise VideoLyricsError(f"Google Docs pointer does not exist: {source}") from exc
    except json.JSONDecodeError as exc:
        raise VideoLyricsError(f"Invalid .gdoc pointer JSON: {source}: {exc}") from exc
    document_id = str(payload.get("doc_id", "")).strip()
    resource_key = str(payload.get("resource_key", "")).strip()
    if not document_id or not re.fullmatch(r"[A-Za-z0-9_-]+", document_id):
        raise VideoLyricsError(f"The .gdoc file does not contain a valid doc_id: {source}")
    return document_id, resource_key


def export_gdoc_text(path: str | Path, *, env_dir: str | Path | None = None) -> str:
    load_project_env(env_dir)
    document_id, resource_key = parse_gdoc(path)
    credentials = _credentials_from_environment()
    access_token = refresh_access_token(**credentials)
    query = urllib.parse.urlencode({"includeTabsContent": "true"})
    url = DOCS_DOCUMENT_ENDPOINT.format(
        document_id=urllib.parse.quote(document_id, safe="")
    )
    headers = {"Authorization": f"Bearer {access_token}"}
    if resource_key:
        headers["X-Goog-Drive-Resource-Keys"] = f"{document_id}/{resource_key}"
    request = urllib.request.Request(f"{url}?{query}", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            document = json.loads(response.read().decode("utf-8-sig"))
    except urllib.error.HTTPError as exc:
        raise _google_http_error("Google Docs could not retrieve the document", exc) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise VideoLyricsError(f"Google Docs retrieval failed: {exc}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise VideoLyricsError(f"Google Docs returned an invalid document response: {exc}") from exc
    if not isinstance(document, dict):
        raise VideoLyricsError("Google Docs response was not a JSON object")
    tabs = document.get("tabs", [])
    if not isinstance(tabs, list) or not tabs or not isinstance(tabs[0], dict):
        raise VideoLyricsError("Google Docs response did not contain a first/main tab")
    first_tab = tabs[0]
    tab_properties = first_tab.get("tabProperties", {})
    tab_title = (
        str(tab_properties.get("title", "")).strip()
        if isinstance(tab_properties, dict)
        else ""
    )
    document_tab = first_tab.get("documentTab", {})
    if not isinstance(document_tab, dict):
        raise VideoLyricsError("Google Docs first/main tab had invalid content")
    body = document_tab.get("body", {})
    if not isinstance(body, dict):
        raise VideoLyricsError("Google Docs response did not contain the main tab body")
    content = body.get("content", [])
    if not isinstance(content, list):
        raise VideoLyricsError("Google Docs main tab body had invalid content")
    return _remove_gdoc_heading_lines(_extract_structural_text(content), tab_title)


def _remove_gdoc_heading_lines(text: str, tab_title: str) -> str:
    """Remove a leaked tab label and the first body line used as the document title."""
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and tab_title and lines[0].strip().casefold() == tab_title.casefold():
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    if lines:
        lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines) + ("\n" if lines else "")


def _extract_structural_text(content: list[Any]) -> str:
    """Extract visible body text from Google Docs structural elements."""
    parts: list[str] = []
    for element in content:
        if not isinstance(element, dict):
            continue
        paragraph = element.get("paragraph")
        if isinstance(paragraph, dict):
            for paragraph_element in paragraph.get("elements", []):
                if not isinstance(paragraph_element, dict):
                    continue
                text_run = paragraph_element.get("textRun")
                if isinstance(text_run, dict):
                    parts.append(str(text_run.get("content", "")))
            continue
        table = element.get("table")
        if isinstance(table, dict):
            for row in table.get("tableRows", []):
                if not isinstance(row, dict):
                    continue
                for cell in row.get("tableCells", []):
                    if not isinstance(cell, dict):
                        continue
                    cell_text = _extract_structural_text(cell.get("content", []))
                    parts.append(cell_text)
                    if cell_text and not cell_text.endswith("\n"):
                        parts.append("\n")
            continue
        table_of_contents = element.get("tableOfContents")
        if isinstance(table_of_contents, dict):
            parts.append(_extract_structural_text(table_of_contents.get("content", [])))
    return "".join(parts)


def refresh_access_token(
    *, client_id: str, client_secret: str, refresh_token: str
) -> str:
    response = _post_form(
        TOKEN_ENDPOINT,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    access_token = str(response.get("access_token", ""))
    if not access_token:
        raise VideoLyricsError("Google OAuth refresh returned no access token")
    return access_token


def authorize_google_drive(
    env_file: str | Path = ".env",
    *,
    timeout: int = 300,
    open_browser: bool = True,
    output: Callable[[str], None] = print,
) -> dict[str, Any]:
    destination = Path(env_file).expanduser().resolve()
    load_env_file(destination)
    client_id = os.environ.get(CLIENT_ID_ENV, "").strip()
    client_secret = os.environ.get(CLIENT_SECRET_ENV, "").strip()
    missing = [
        name
        for name, value in ((CLIENT_ID_ENV, client_id), (CLIENT_SECRET_ENV, client_secret))
        if not value
    ]
    if missing:
        raise VideoLyricsError(
            f"Missing {', '.join(missing)}. Add the Desktop OAuth client values to {destination}."
        )

    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    server = HTTPServer(("127.0.0.1", 0), _OAuthCallbackHandler)
    server.timeout = max(1, int(timeout))
    server.expected_state = state  # type: ignore[attr-defined]
    redirect_uri = f"http://127.0.0.1:{server.server_port}/oauth2callback"
    authorization_url = _authorization_url(client_id, redirect_uri, state, challenge)

    output("Authorize read-only Google Drive access in your browser.")
    opened = open_browser and webbrowser.open(authorization_url, new=1, autoraise=True)
    if not opened:
        output(f"Open this URL manually:\n{authorization_url}")
    try:
        server.handle_request()
        result = getattr(server, "oauth_result", None)
    finally:
        server.server_close()
    if not result:
        raise VideoLyricsError(f"Google OAuth timed out after {timeout} seconds")
    if result.get("error"):
        raise VideoLyricsError(f"Google OAuth authorization failed: {result['error']}")
    code = str(result.get("code", ""))
    if not code:
        raise VideoLyricsError("Google OAuth callback did not include an authorization code")

    tokens = _post_form(
        TOKEN_ENDPOINT,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
    )
    refresh_token = str(tokens.get("refresh_token", ""))
    if not refresh_token:
        raise VideoLyricsError(
            "Google did not return a refresh token. Revoke the previous app grant and rerun "
            "`video-lyrics google-auth`; the command already requests prompt=consent."
        )
    update_env_file(destination, {REFRESH_TOKEN_ENV: refresh_token})
    os.environ[REFRESH_TOKEN_ENV] = refresh_token
    return {"env_file": str(destination), "scope": DRIVE_READONLY_SCOPE, "refresh_token_saved": True}


def _authorization_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    parameters = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": DRIVE_READONLY_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZATION_ENDPOINT}?{urllib.parse.urlencode(parameters)}"


def _credentials_from_environment() -> dict[str, str]:
    values = {
        "client_id": os.environ.get(CLIENT_ID_ENV, "").strip(),
        "client_secret": os.environ.get(CLIENT_SECRET_ENV, "").strip(),
        "refresh_token": os.environ.get(REFRESH_TOKEN_ENV, "").strip(),
    }
    missing = [
        environment_name
        for key, environment_name in (
            ("client_id", CLIENT_ID_ENV),
            ("client_secret", CLIENT_SECRET_ENV),
            ("refresh_token", REFRESH_TOKEN_ENV),
        )
        if not values[key]
    ]
    if missing:
        raise VideoLyricsError(
            "Missing Google OAuth configuration: "
            + ", ".join(missing)
            + ". Add the client values to .env and run `video-lyrics google-auth`."
        )
    return values


def _post_form(url: str, values: dict[str, str]) -> dict[str, Any]:
    body = urllib.parse.urlencode(values).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise _google_http_error("Google OAuth token request failed", exc) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise VideoLyricsError(f"Google OAuth token request failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise VideoLyricsError("Google OAuth token response was not a JSON object")
    return payload


def _google_http_error(prefix: str, error: urllib.error.HTTPError) -> VideoLyricsError:
    detail = ""
    try:
        payload = json.loads(error.read().decode("utf-8"))
        if isinstance(payload, dict):
            raw_error = payload.get("error")
            if isinstance(raw_error, dict):
                detail = str(raw_error.get("message", ""))
            else:
                detail = str(payload.get("error_description") or raw_error or "")
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    suffix = f": {detail}" if detail else ""
    return VideoLyricsError(f"{prefix} (HTTP {error.code}){suffix}")


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        expected_state = getattr(self.server, "expected_state", "")
        state = query.get("state", [""])[0]
        if parsed.path != "/oauth2callback":
            self.send_error(404)
            return
        if not secrets.compare_digest(state, expected_state):
            self.server.oauth_result = {"error": "OAuth state mismatch"}  # type: ignore[attr-defined]
            self._respond(400, "Authorization failed. Return to the terminal.")
            return
        error = query.get("error", [""])[0]
        code = query.get("code", [""])[0]
        self.server.oauth_result = {"error": error, "code": code}  # type: ignore[attr-defined]
        if error:
            self._respond(400, "Authorization was not granted. Return to the terminal.")
        else:
            self._respond(200, "Authorization complete. You can close this tab.")

    def _respond(self, status: int, message: str) -> None:
        body = (
            "<!doctype html><html><head><meta charset='utf-8'><title>Video Lyrics Creator</title>"
            f"</head><body><h1>{message}</h1></body></html>"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return

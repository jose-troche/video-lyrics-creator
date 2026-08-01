# Google Drive OAuth for `.gdoc` lyrics

Google Drive Desktop `.gdoc` files are small JSON pointers. They contain a `doc_id` and sometimes a `resource_key`; they do not contain the Google Doc’s text. Video Lyrics Creator uses Google OAuth and the Google Docs API to retrieve private document text when lyrics are prepared.

## Google Cloud configuration

1. Open the Google Cloud Console and create or select a project.
2. Enable the **Google Docs API** for that project.
3. Configure the Google Auth platform/OAuth consent screen.
4. For an external app in Testing, add the Google account that owns the song documents as a test user.
5. Create a client under **Credentials > OAuth client ID > Desktop app**.
6. Copy the client ID and client secret into the project’s ignored `.env` file.

Do not create a Web application client. The CLI uses Google’s installed-app flow with a random loopback address such as `http://127.0.0.1:54321/oauth2callback`.

## Environment variables

```dotenv
GOOGLE_DRIVE_CLIENT_ID=
GOOGLE_DRIVE_CLIENT_SECRET=
GOOGLE_DRIVE_REFRESH_TOKEN=
```

Run:

```bash
video-lyrics google-auth
```

The command:

1. Generates a cryptographically random OAuth state and PKCE verifier/challenge.
2. Starts a temporary HTTP listener on `127.0.0.1` using a random available port.
3. Opens Google’s authorization page requesting `https://www.googleapis.com/auth/drive.readonly`.
4. Validates the callback state and exchanges the authorization code for tokens.
5. Updates only `GOOGLE_DRIVE_REFRESH_TOKEN` in `.env` and restricts the file to the current user where supported.

Use `video-lyrics google-auth --no-browser` to print the authorization URL when a browser cannot be opened automatically. The callback still needs to reach the same computer within the default five-minute timeout.

## Runtime behavior

When `lyrics_source` ends in `.gdoc`, the terminal process:

1. Parses and validates the pointer’s document ID.
2. Loads OAuth values from `.env` beside `project.json` or from the current directory.
3. Exchanges the refresh token for a short-lived access token.
4. Calls `GET https://docs.googleapis.com/v1/documents/{documentId}?includeTabsContent=true`, selects only the first/main tab, and reads its body.
5. Removes the tab name when it appears in the body, then always removes the first remaining body line as the document/song title.
6. Ignores headers, footers, footnotes, child tabs, and every other document tab.
7. Applies the common lyric cleanup rules: removes complete square-bracket annotations, empty lines, and underscore-only separators such as `________`.
8. Supplies the remaining lines as reviewed wording for audio-confirmed lyric cues. `[Verse 1]` is discarded, while `Amazing [quietly] grace` becomes `Amazing grace`.

OAuth secrets never enter `project.json`, `resolve-job.json`, scene prompts, logs, or the Resolve scripts directory.

## Security and token lifetime

The `drive.readonly` scope can view and download all Drive files and is classified by Google as restricted. This implementation does not upload, edit, share, or delete Drive content. Use a dedicated personal OAuth project and do not distribute its client credentials.

Google states that refresh tokens for external OAuth consent screens in Testing expire after seven days unless only basic identity scopes are requested. Since Drive access is requested, rerun `video-lyrics google-auth` after expiration or configure the consent project appropriately for long-term personal use.

Revoke access at any time from the Google account’s third-party access settings. Remove `GOOGLE_DRIVE_REFRESH_TOKEN` from `.env` afterward.

## Troubleshooting

### Missing client credentials

Copy `.env.example` to `.env`, set the Desktop client ID and secret, then rerun `video-lyrics google-auth`.

### Access blocked or redirect mismatch

Confirm the OAuth client type is **Desktop app**, not Web application, and that your account is included as a test user.

### Google Docs API has not been used or is disabled

Enable the Google Docs API in the same Google Cloud project that owns the OAuth client.

### `invalid_grant` or expired refresh token

Rerun `video-lyrics google-auth`. If Google does not return a new refresh token, revoke the app’s existing account grant first, then authorize again.

### The document is empty

Open the Google Doc's first/main tab and confirm its first body line is the document/song title followed by ordinary lyric text. The tab name, title line, blank lines, underscore-only separators, square-bracket annotations, and every other tab are intentionally ignored.

## Official references

- [OAuth 2.0 for desktop applications](https://developers.google.com/identity/protocols/oauth2/native-app)
- [Google Docs `documents.get`](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/get)
- [Work with Google Docs tabs](https://developers.google.com/workspace/docs/api/how-tos/tabs)
- [Drive API scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)

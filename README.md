# Send to Library

A small self-hosted FastAPI service that accepts an Archive of Our Own work URL, downloads the work as an EPUB with FanFicFare, imports or updates it in a Calibre library, and tracks AO3 work IDs in SQLite to avoid duplicates.

Run it only on a trusted private network, such as Tailscale, WireGuard, or your home LAN. Do not expose it directly to the public internet.

## Files

- `app/main.py`: FastAPI routes and request orchestration
- `app/db.py`: SQLite state for AO3 work IDs and Calibre book IDs
- `app/downloader.py`: AO3 URL validation and FanFicFare execution
- `app/calibre.py`: `calibredb` import/update helpers
- `tests/`: URL and auth behavior tests

## Configure

Edit `docker-compose.yml` and replace:

```yaml
- /path/to/your/Calibre Library:/library
```

with the host path to your Calibre library. The container needs read/write access to that directory.

Useful environment variables:

```env
API_TOKEN=change-me
TZ=America/New_York
AO3_AUTH_MODE=none
```

If `API_TOKEN` is set, API requests must include `Authorization: Bearer <token>`. The browser form at `/` is intended for private-network use.

## Run

```bash
docker compose up --build -d
```

Open:

```text
http://localhost:8000/
```

## Test With Curl

Without an API token:

```bash
curl -X POST http://localhost:8000/add \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://archiveofourown.org/works/123456"}'
```

With an API token:

```bash
curl -X POST http://localhost:8000/add \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer change-me' \
  -d '{"url":"https://archiveofourown.org/works/123456"}'
```

The response includes `title`, `author`, `work_id`, `calibre_book_id`, and whether the work was `added` or `updated`.

## Authenticated AO3 Downloads

Some AO3 works require your account session. Cookie-based auth is preferred because it avoids storing your AO3 password in the app.

1. Export your own AO3 browser cookies in Netscape `cookies.txt` format.
2. Save them at `./config/cookies.txt`.
3. Set:

```env
AO3_AUTH_MODE=cookies
```

4. Mount the file read-only in `docker-compose.yml`:

```yaml
volumes:
  - ./config/cookies.txt:/config/cookies.txt:ro
```

When AO3 expires your session, export fresh cookies and replace the file. The service never logs cookie values.

Credential auth is intentionally minimal and should be avoided unless you understand the tradeoff. If enabled, set `AO3_AUTH_MODE=credentials` and mount Docker secrets or files, then point `AO3_USERNAME_FILE` and `AO3_PASSWORD_FILE` at those files. Credentials are not stored in SQLite or shown in the UI.

This app does not bypass AO3 access controls. It only supports downloading works that your own AO3 account can access. Keep request rates low; the default download delay is two seconds.

## Phone Share Setup

### iOS Shortcut With Progress

Create a shortcut that appears in the share sheet. Use the async endpoint so the phone gets a response immediately, then polls the status endpoint while the server downloads and imports the EPUB.

1. Receive `URLs` from share sheet.
2. Add `Get Contents of URL`.
3. URL: `http://your-server:8000/add-async`
4. Method: `POST`
5. Headers:
   - `Content-Type`: `application/json`
   - `Authorization`: `Bearer change-me` if `API_TOKEN` is set
6. Request body JSON:

```json
{"url":"Shortcut Input"}
```

7. Save `status_url` from the response dictionary.
8. Repeat until the returned `done` value is true:
   - Wait 2 seconds.
   - Get Contents of `status_url` with method `GET`.
   - Show notification or update progress text using `progress`, `status`, and `message`.
9. When `done` is true, show `title` if `ok` is true, otherwise show `message`.

The `progress` value is coarse server-side stage progress: queued is 5, downloading is 35, importing is 80, and done or error is 100. FanFicFare does not expose reliable byte-level progress here, but this avoids a single long-running phone request with no visible state. The original `/add` endpoint still works for clients that prefer to wait for the final response.

### Firefox or Safari

If your browser cannot POST directly from the share sheet, make a shortcut/bookmarklet that sends the current page URL to `/add`, or paste the AO3 URL into the mobile-friendly form at `/`.

## Local Tests

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pytest
```

## Error Handling

The service returns:

- `400` for invalid or non-AO3 URLs
- `401` for missing/invalid API token, or AO3 auth required/expired
- `502` for FanFicFare download failures
- `500` for Calibre import failures, missing library path, or volume permission errors

Failures are recorded in SQLite and shown on the recent downloads table.

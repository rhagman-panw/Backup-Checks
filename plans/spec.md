# Spec: Panorama Config Bundle Search Tool

## Objective
A local Python web app that lets an engineer:
1. Connect to Panorama and download the **Panorama and devices config bundle** (.tgz), **or** upload a .tgz bundle they already have.
2. Unpack the bundle safely.
3. Search every config file in it for one or more values the user enters (IP, object name, rule name, and so on).
4. See each match with N lines of context (user can change N, default 3), with the **firewall/device name** shown.
5. Never see credentials: the password field is masked, credentials are never logged or saved, and secret values inside the configs are redacted in the results.
6. Export the (already redacted) results as **CSV** or **plain text**.

## Assumptions (confirmed)
- Flask + Jinja2 web front end, runs on `127.0.0.1` only.
- Auth: username/password → API key via `type=keygen` (sent with POST so credentials stay out of URLs and logs). The API key is kept in memory only, per request.
- SSL verification is on by default. A checkbox turns it off for lab or self-signed Panorama.
- Upload mode needs no Panorama connection.

## Tech Stack
- Python 3.10+
- Flask, requests, lxml (or stdlib `xml.etree`), defusedxml
- pytest for tests

## Commands
```
Install: python -m venv .venv && .venv\Scripts\activate && pip install -r requirements.txt
Run:     python app.py            (serves http://127.0.0.1:5000)
Test:    pytest -q
```

## Project Structure
```
app.py                  Flask routes, app factory, in-memory result store
bundlesearch/
  client.py             keygen, export bundle, show devices (serial→hostname)
  bundle.py             safe tar extraction, file discovery, device name resolution
  search.py             line search with context, multi-term, regex/case options
  redact.py             secret masking for XML text
  report.py             run a search over a bundle; CSV / text export
templates/
  base.html, index.html, results.html
static/
  style.css, app.js
tests/
  conftest.py           synthetic configs + tgz builder (no real data)
  test_redact.py, test_search.py, test_bundle.py, test_client.py, test_app.py
requirements.txt
pytest.ini
README.md
```

## Key Technical Design
- **Keygen:** `POST https://<host>/api/` with `type=keygen&user=..&password=..`.
- **Export bundle:** `GET https://<host>/api/?type=export&category=device-config-bundle` with header `X-PAN-KEY`, saved by streaming to a temp file. *The category name must be checked against the target PAN-OS version (API browser at `https://<host>/api` or `debug cli on`). Keep it as one constant in `client.py`.*
- **Device names:** when connected, call op `<show><devices><all/></devices></show>` to map serial → hostname. If that fails or for uploaded bundles, read the hostname from each file's `deviceconfig/system/hostname`. If neither works, use the file name or serial. Panorama's own file is labelled `Panorama (<hostname>)`.
- **Safe extraction:** reject absolute paths, `..`, symlinks and hardlinks; cap total extracted size; extract into a `tempfile.TemporaryDirectory` that is deleted after each search.
- **Redaction:** before results are shown, mask the contents of sensitive XML elements/attributes such as `password`, `phash`, `key`, `secret`, `pre-shared-key`, `auth-key`, `private-key`, `passphrase`, `community`, `authpwd`, `privpwd`, `api-key`, `bind-password`, replacing them with `********`. The match runs on redacted text, so searching for a secret returns nothing.
- **Search:** multiple terms (one per line), optional case sensitivity and regex. Results are grouped by device, then term, with line numbers and the matched text highlighted (HTML-escaped).
- **Credential hygiene:** `<input type="password" autocomplete="off">`, no credentials in session, cookies, logs, error messages or tracebacks. Flask debug is off by default. Secret key is generated at random on startup.
- **Export:** redacted results are kept in memory under a random token (1 hour TTL, at most 20 reports). `GET /export/<token>/csv|txt` downloads them. CSV has one row per matching line (Device, File, Line, Search Terms, Matched Line, Context). Cells are protected against spreadsheet formula injection. Text output looks like grep with `>` marking matched lines.

## Code Style
PEP 8, type hints, small pure functions in `search.py` / `redact.py` so they are easy to test.
```python
def search_lines(lines: list[str], pattern: re.Pattern, context: int) -> list[Match]:
    ...
```

## Testing Strategy
- Unit: redaction patterns, search context edges (start or end of file, overlapping windows merged), tar-slip rejection, device name resolution.
- Client: `requests` mocked (responses or monkeypatch). No live Panorama in tests.
- Manual: run against a real Panorama in a lab.

## Boundaries
- Always: redact before showing results, delete temp files, HTML-escape output, bind to localhost.
- Ask first: new dependencies beyond those listed, saving bundles to disk permanently, binding to 0.0.0.0.
- Never: log or save credentials or API keys, put credentials in URLs, commit real configs.

## Success Criteria
- Can download a bundle from Panorama with username/password and search it.
- Can upload a .tgz and search it with no Panorama access.
- Each result shows device name, file, line number and ±N context lines.
- No password, API key or config secret appears in the UI, logs or HTTP query strings.
- Malicious tar paths are rejected (tested).
- `pytest` passes.

## Open Questions
- Exact export `category` value for your PAN-OS version (to be checked in the lab).
- ~~Should results be exportable as CSV/text?~~ Yes, added.

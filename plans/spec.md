# Spec: Panorama Config Search Tool

## Objective
A local Python web app that lets an engineer:
1. Get configurations from one of three sources:
   - **Panorama device list**: sign in to Panorama, read the list of connected firewalls and pull each firewall's running config. Firewalls can be reached **through Panorama** (`target=<serial>`) or **directly** at their management IP. Panorama's own running config can be included.
   - **Linux server**: SSH to a server and download the **newest** stored Panorama bundle (SFTP, or SCP when needed).
   - **Upload**: a .tgz bundle the user already has.
2. Unpack bundles safely.
3. Search every config for one or more user-supplied values.
4. Show each match with N lines of context (user-adjustable, default 3) and the **firewall/device name**.
5. Never show credentials: masked inputs, nothing saved or logged, and secrets in configs redacted.
6. Export the (already redacted) results as **CSV** or **plain text**.

## Tech Stack
Python 3.10+, Flask, requests, defusedxml, paramiko, scp, pytest.

## Commands
```
Install: python -m venv .venv && .venv\Scripts\activate && pip install -r requirements.txt
Run:     python app.py            (serves http://127.0.0.1:5000)
Test:    pytest
```

## Project Structure
```
app.py                  Flask routes, source handlers, in-memory result store
bundlesearch/
  client.py             PAN-OS XML API: keygen, connected devices, running config (target=serial)
  collector.py          device filter; collect via Panorama proxy or direct, in parallel, errors per device
  remote.py             SSH (password/key/agent), host key policy, newest bundle, SFTP/SCP
  bundle.py             safe tar extraction, file discovery, device name resolution
  search.py             line search with context, multi-term, regex/case options
  redact.py             secret masking for XML text
  report.py             run a search over a folder; CSV / text export
templates/ static/      UI (three source tabs, results, exports)
tests/                  synthetic fixtures; client, collector, remote, bundle, search, redact, app
```

## Key Technical Design
- **Collected layout:** `panorama/running-config.xml` and `devices/<serial>/running-config.xml`, which the same search code handles as bundle contents.
- **Device names:** serial → hostname from Panorama's connected list, then the config's own `deviceconfig/system/hostname`, then the serial or file name.
- **Proxy mode:** clones the Panorama client per thread (shared API key, separate HTTP session).
- **Direct mode:** one client per firewall at its management IP (IPv4, or else IPv6). Firewall credentials default to the Panorama ones.
- **Partial failures:** problems with individual firewalls are collected into `Report.errors` and shown on the page and in the text export. The run only fails if nothing at all could be collected.
- **SSH:** `RejectPolicy` by default (`AutoAddPolicy` only when the user ticks the box). Agent and `~/.ssh` keys are used only when no password or key is given. The remote folder is `shlex.quote`d for `ls`/`stat` in SCP mode. Size limits are checked before and after download.
- **Redaction, safe extraction, credential hygiene, exports:** unchanged from v1.

## Boundaries
- Always: redact before showing results, delete temp files, HTML-escape output, bind to localhost, verify SSL and host keys by default.
- Ask first: new dependencies, saving configs permanently, binding to 0.0.0.0.
- Never: log or save credentials, API keys or private keys; put credentials in URLs; commit real configs.

## Success Criteria
- Panorama device list mode collects from connected firewalls in both proxy and direct modes, and the device filter works.
- Linux mode downloads the newest matching bundle over SFTP or SCP.
- Upload mode still works.
- Each result shows device name, file, line number and ±N context lines; CSV/text export works.
- No secret appears in the UI, logs, exports or query strings.
- `pytest` passes.

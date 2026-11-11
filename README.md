# Panorama Config Bundle Search

A local web app that downloads the **Panorama and devices config bundle** from Panorama (or takes a `.tgz` you upload), unpacks it safely, and searches every config for the values you enter. Each result shows the **firewall/device name**, file, line number and lines of context around the match. Results can be exported as **CSV** or **plain text**.

## Setup

```cmd
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```cmd
python app.py
```

Open http://127.0.0.1:5000. The app only listens on localhost. Use `--host` / `--port` to change this; ask before exposing it on a network.

## Usage

1. Choose **Download from Panorama** (host, username, password) or **Upload existing .tgz**.
2. Enter search values, one per line (IPs, object names, rule names, and so on).
3. Set the number of context lines (0–20) and, if needed, turn on case sensitivity or regex.
4. Results are grouped by device. Use **Export CSV** or **Export Text** to download them.

## Credential and secret handling

- The password field is masked. It is never sent back to the page, saved, logged, or put in a URL.
- Credentials are sent once in a POST body to get an API key. The key is kept only in memory and sent in the `X-PAN-KEY` header.
- Error messages are scrubbed of passwords and API keys.
- Secret values in the configs (passwords, phash, keys, PSKs, SNMP communities, and similar) are replaced with `********` **before** searching. They never show up in results or exports, and searching for them returns nothing.
- Bundles are extracted to a temporary folder that is deleted after each search. Exports are kept in memory for 1 hour.
- Extraction rejects path traversal, skips symlinks and hardlinks, and caps the total extracted size.

## PAN-OS note

The export uses `type=export&category=device-config-bundle`. If your PAN-OS version uses a different category, change `EXPORT_CATEGORY` in [`bundlesearch/client.py`](bundlesearch/client.py). You can check it with the API browser at `https://<panorama>/api`, or with `debug cli on` while running the export from the CLI.

## Tests

```cmd
pytest
```

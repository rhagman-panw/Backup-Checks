<<<<<<< HEAD
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
=======
# Panorama Config Search

A local web app that collects PAN-OS configurations, unpacks them safely, and searches every config for the values you enter. Each result shows the **firewall/device name**, file, line number and lines of context around the match. Results can be exported as **CSV** or **plain text**.

## Configuration sources

| Source | What it does |
|---|---|
| **Panorama device list** | Signs in to Panorama, reads the list of **connected** firewalls (`show devices connected`), and pulls each firewall's running config. You can include Panorama's own running config too. |
| **Linux server (latest bundle)** | Connects to a Linux server over SSH and downloads the **newest** file matching a pattern (default `*.tgz`) from the folder where Panorama bundles are stored. Uses SFTP, or SCP when SFTP isn't available. |
| **Upload existing .tgz** | Searches a bundle you already have. |

### Firewall connectivity (Panorama device list)

- **Proxy through Panorama** (default): each firewall's config is requested from Panorama with `target=<serial>`, and Panorama forwards the request to the firewall. Only Panorama needs to be reachable, and only Panorama credentials are used.
- **Direct**: the app connects to each firewall's management IP (as reported by Panorama) with firewall credentials. If you leave those blank, the Panorama credentials are used.

You can limit which firewalls are collected with a **device filter**: hostnames or serials, one per line, with `*` wildcards. Up to 8 firewalls are collected at once. If a firewall can't be reached, it's listed on the results page and in the text export; the rest of the run carries on.

### Linux server

- Sign in with a password, an uploaded private key (with an optional passphrase), or neither. With neither, this computer's SSH agent and `~/.ssh` keys are used.
- Host keys are checked against the system `known_hosts`, and unknown hosts are **rejected** unless you tick *Accept unknown host key*.
- The newest matching file is picked by modification time. Transfer can be *Auto* (SFTP, falling back to SCP), *SFTP* or *SCP*. In SCP mode the file is found with `ls` and the folder path is shell-quoted.

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

1. Choose a source and fill in its connection details.
2. Enter search values, one per line (IPs, object names, rule names, and so on).
3. Set the number of context lines (0–20) and, if needed, turn on case sensitivity or regex.
4. Results are grouped by device. Use **Export CSV** or **Export Text** to download them.

## Credential and secret handling

- All password and passphrase fields are masked and cleared after you submit. They're never sent back to the page, saved, logged, or put in a URL. An uploaded private key is read into memory only.
- PAN-OS credentials are sent once in a POST body to get an API key. The key is kept only in memory and sent in the `X-PAN-KEY` header.
- Error messages are scrubbed of passwords and API keys. SSH errors are turned into generic messages.
- Secret values in the configs (passwords, phash, keys, PSKs, SNMP communities, and similar) are replaced with `********` **before** searching. They never show up in results or exports, and searching for them returns nothing.
- Downloads and extracted files go to a temporary folder that's deleted after each search. Exports are kept in memory for 1 hour.
- Extraction rejects path traversal, skips symlinks and hardlinks, and caps the total extracted size (4 GiB). Downloads from the Linux server are capped at 2 GiB.

## PAN-OS API calls used

| Purpose | Call |
|---|---|
| API key | `type=keygen` (POST) |
| Connected devices | `type=op&cmd=<show><devices><connected/></devices></show>` |
| Running config | `type=config&action=show&xpath=/config` (add `&target=<serial>` to proxy through Panorama) |

The API user needs XML API permission for *Operational Requests* and *Configuration*. Proxied requests need Panorama to have a working connection to each firewall.

## Tests

```cmd
pytest
```
>>>>>>> dev

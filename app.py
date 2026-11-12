<<<<<<< HEAD
"""Web front end for downloading and searching Panorama config bundles.

Run:  python app.py   ->  http://127.0.0.1:5000
"""
from __future__ import annotations

import argparse
import logging
import os
import secrets
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path

from flask import Flask, Response, abort, flash, redirect, render_template, request, url_for

from bundlesearch.bundle import BundleError, extract_bundle
from bundlesearch.client import PanoramaClient, PanoramaError
from bundlesearch.report import Report, run_search, to_csv, to_text
from bundlesearch.search import MAX_CONTEXT, build_matchers, highlight, parse_context, parse_terms

log = logging.getLogger("bundlesearch")

REPORT_TTL_SECONDS = 3600
MAX_REPORTS = 20
ALLOWED_UPLOAD_SUFFIXES = (".tgz", ".tar.gz", ".tar")


class ReportStore:
    """Small thread-safe in-memory store for redacted reports (for export downloads)."""

    def __init__(self, ttl: int = REPORT_TTL_SECONDS, max_items: int = MAX_REPORTS) -> None:
        self.ttl, self.max_items = ttl, max_items
        self._items: OrderedDict[str, tuple[float, Report]] = OrderedDict()
        self._lock = threading.Lock()

    def _purge(self) -> None:
        now = time.monotonic()
        for token in [t for t, (ts, _) in self._items.items() if now - ts > self.ttl]:
            del self._items[token]
        while len(self._items) > self.max_items:
            self._items.popitem(last=False)

    def put(self, report: Report) -> str:
        token = secrets.token_urlsafe(16)
        with self._lock:
            self._items[token] = (time.monotonic(), report)
            self._purge()
        return token

    def get(self, token: str) -> Report | None:
        with self._lock:
            self._purge()
            item = self._items.get(token)
            return item[1] if item else None


def _checkbox(name: str) -> bool:
    return request.form.get(name) in ("on", "1", "true", "yes")


def _form_defaults() -> dict:
    """Non-secret form values to re-fill the form. Password is never echoed back."""
    f = request.form
    return {
        "mode": f.get("mode", "panorama"),
        "host": f.get("host", ""),
        "username": f.get("username", ""),
        "verify_ssl": _checkbox("verify_ssl") if f else True,
        "terms": f.get("terms", ""),
        "context": f.get("context", "3"),
        "regex": _checkbox("regex"),
        "case_sensitive": _checkbox("case_sensitive"),
    }


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secrets.token_hex(32),  # random per run; only used for flash messages
        MAX_CONTENT_LENGTH=2 * 1024**3,  # 2 GiB upload cap
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        CLIENT_FACTORY=PanoramaClient,
        MAX_EXTRACT_BYTES=4 * 1024**3,
    )
    if config:
        app.config.update(config)
    store = ReportStore()
    app.extensions["report_store"] = store
    app.jinja_env.globals.update(highlight=highlight, MAX_CONTEXT=MAX_CONTEXT)

    @app.after_request
    def _security_headers(resp: Response) -> Response:
        resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:",
        )
        return resp

    @app.errorhandler(413)
    def _too_large(_e):
        flash("Uploaded file is too large.", "error")
        return render_template("index.html", form=_form_defaults()), 413

    @app.get("/")
    def index():
        return render_template("index.html", form=_form_defaults())

    @app.post("/search")
    def search():
        form = _form_defaults()

        def fail(message: str, status: int = 400):
            flash(message, "error")
            return render_template("index.html", form=form), status

        try:
            terms = parse_terms(form["terms"])
            context = parse_context(form["context"])
            matchers = build_matchers(
                terms, regex=form["regex"], case_sensitive=form["case_sensitive"]
            )
        except ValueError as exc:
            return fail(str(exc))

        with tempfile.TemporaryDirectory(prefix="bundlesearch-") as tmp:
            tmpdir = Path(tmp)
            archive = tmpdir / "bundle.tgz"
            serial_map: dict[str, str] = {}

            if form["mode"] == "upload":
                upload = request.files.get("bundle")
                if not upload or not upload.filename:
                    return fail("Choose a .tgz config bundle to upload.")
                if not upload.filename.lower().endswith(ALLOWED_UPLOAD_SUFFIXES):
                    return fail("Upload must be a .tgz, .tar.gz or .tar file.")
                upload.save(archive)
                source = f"Uploaded file: {Path(upload.filename).name}"
            else:
                password = request.form.get("password", "")
                if not form["host"].strip() or not form["username"].strip() or not password:
                    return fail("Panorama host, username and password are required.")
                try:
                    client = app.config["CLIENT_FACTORY"](
                        form["host"], verify=form["verify_ssl"]
                    )
                    client.login(form["username"].strip(), password)
                    del password
                    try:
                        serial_map = client.get_device_map()
                    except PanoramaError as exc:
                        log.warning("Could not get device list; using config hostnames: %s", exc)
                    client.export_bundle(archive)
                except PanoramaError as exc:
                    return fail(str(exc), 502)
                source = f"Panorama {client.host}"

            try:
                root = extract_bundle(
                    archive, tmpdir / "extracted",
                    max_total_bytes=app.config["MAX_EXTRACT_BYTES"],
                )
                archive.unlink(missing_ok=True)
                report = run_search(
                    root, matchers,
                    context=context,
                    regex=form["regex"],
                    case_sensitive=form["case_sensitive"],
                    serial_map=serial_map,
                    source=source,
                )
            except BundleError as exc:
                return fail(str(exc))

        token = store.put(report)
        return render_template("results.html", report=report, matchers=matchers, token=token)

    @app.get("/export/<token>/<fmt>")
    def export(token: str, fmt: str):
        report = store.get(token)
        if report is None:
            flash("These results have expired. Run the search again.", "error")
            return redirect(url_for("index"))
        stamp = report.created.strftime("%Y%m%d-%H%M%S")
        if fmt not in ("csv", "txt"):
            abort(404)
        if fmt == "csv":
            body, mime = "\ufeff" + to_csv(report), "text/csv"  # BOM so Excel detects UTF-8
        else:
            body, mime = to_text(report), "text/plain"
        return Response(
            body,
            mimetype=mime,
            headers={
                "Content-Disposition": f'attachment; filename="bundle-search-{stamp}.{fmt}"'
            },
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Panorama config bundle search web UI")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Do not log request bodies; werkzeug only logs method + path (no credentials in URLs).
    create_app().run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
=======
"""Web front end for collecting and searching Panorama / firewall configurations.

Sources:
  * panorama - get the connected device list from Panorama, then pull each
               firewall's running config (proxied through Panorama or direct)
  * linux    - SSH to a Linux server and download the newest stored bundle (SFTP/SCP)
  * upload   - upload a .tgz bundle

Run:  python app.py   ->  http://127.0.0.1:5000
"""
from __future__ import annotations

import argparse
import logging
import os
import secrets
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path

from flask import Flask, Response, abort, flash, redirect, render_template, request, url_for

from bundlesearch.bundle import BundleError, extract_bundle
from bundlesearch.client import PanoramaClient, PanoramaError
from bundlesearch.collector import METHOD_DIRECT, METHOD_PROXY, collect_configs, parse_filters
from bundlesearch.remote import (
    DEFAULT_PATTERN,
    MAX_KEY_BYTES,
    TRANSFER_AUTO,
    TRANSFERS,
    RemoteBundleFetcher,
    RemoteError,
    load_private_key,
)
from bundlesearch.report import Report, run_search, to_csv, to_text
from bundlesearch.search import MAX_CONTEXT, build_matchers, highlight, parse_context, parse_terms

log = logging.getLogger("bundlesearch")

REPORT_TTL_SECONDS = 3600
MAX_REPORTS = 20
ALLOWED_UPLOAD_SUFFIXES = (".tgz", ".tar.gz", ".tar")
MODES = ("panorama", "linux", "upload")


class ReportStore:
    """Small thread-safe in-memory store for redacted reports (for export downloads)."""

    def __init__(self, ttl: int = REPORT_TTL_SECONDS, max_items: int = MAX_REPORTS) -> None:
        self.ttl, self.max_items = ttl, max_items
        self._items: OrderedDict[str, tuple[float, Report]] = OrderedDict()
        self._lock = threading.Lock()

    def _purge(self) -> None:
        now = time.monotonic()
        for token in [t for t, (ts, _) in self._items.items() if now - ts > self.ttl]:
            del self._items[token]
        while len(self._items) > self.max_items:
            self._items.popitem(last=False)

    def put(self, report: Report) -> str:
        token = secrets.token_urlsafe(16)
        with self._lock:
            self._items[token] = (time.monotonic(), report)
            self._purge()
        return token

    def get(self, token: str) -> Report | None:
        with self._lock:
            self._purge()
            item = self._items.get(token)
            return item[1] if item else None


class SourceError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _checkbox(name: str) -> bool:
    return request.form.get(name) in ("on", "1", "true", "yes")


def _form_defaults() -> dict:
    """Non-secret form values to re-fill the form. Passwords / keys are never echoed back."""
    f = request.form
    submitted = bool(f)
    mode = f.get("mode", "panorama")
    method = f.get("method", METHOD_PROXY)
    transfer = f.get("transfer", TRANSFER_AUTO)
    return {
        "mode": mode if mode in MODES else "panorama",
        # Panorama
        "host": f.get("host", ""),
        "username": f.get("username", ""),
        "verify_ssl": _checkbox("verify_ssl") if submitted else True,
        "method": method if method in (METHOD_PROXY, METHOD_DIRECT) else METHOD_PROXY,
        "fw_username": f.get("fw_username", ""),
        "device_filter": f.get("device_filter", ""),
        "include_panorama": _checkbox("include_panorama") if submitted else True,
        # Linux server
        "ssh_host": f.get("ssh_host", ""),
        "ssh_port": f.get("ssh_port", "22"),
        "ssh_username": f.get("ssh_username", ""),
        "remote_dir": f.get("remote_dir", ""),
        "remote_pattern": f.get("remote_pattern", DEFAULT_PATTERN),
        "transfer": transfer if transfer in TRANSFERS else TRANSFER_AUTO,
        "accept_unknown_host": _checkbox("accept_unknown_host"),
        # Search
        "terms": f.get("terms", ""),
        "context": f.get("context", "3"),
        "regex": _checkbox("regex"),
        "case_sensitive": _checkbox("case_sensitive"),
    }


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secrets.token_hex(32),  # random per run; only used for flash messages
        MAX_CONTENT_LENGTH=2 * 1024**3,  # 2 GiB upload cap
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        CLIENT_FACTORY=PanoramaClient,
        REMOTE_FACTORY=RemoteBundleFetcher,
        MAX_EXTRACT_BYTES=4 * 1024**3,
        MAX_DOWNLOAD_BYTES=2 * 1024**3,
        COLLECT_WORKERS=8,
    )
    if config:
        app.config.update(config)
    store = ReportStore()
    app.extensions["report_store"] = store
    app.jinja_env.globals.update(highlight=highlight, MAX_CONTEXT=MAX_CONTEXT)

    @app.after_request
    def _security_headers(resp: Response) -> Response:
        resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:",
        )
        return resp

    @app.errorhandler(413)
    def _too_large(_e):
        flash("Uploaded file is too large.", "error")
        return render_template("index.html", form=_form_defaults()), 413

    # ------------------------------------------------------------- sources
    def _from_upload(archive: Path) -> str:
        upload = request.files.get("bundle")
        if not upload or not upload.filename:
            raise SourceError("Choose a .tgz config bundle to upload.")
        if not upload.filename.lower().endswith(ALLOWED_UPLOAD_SUFFIXES):
            raise SourceError("Upload must be a .tgz, .tar.gz or .tar file.")
        upload.save(archive)
        return f"Uploaded file: {Path(upload.filename).name}"

    def _from_linux(form: dict, archive: Path) -> str:
        password = request.form.get("ssh_password", "")
        key_file = request.files.get("ssh_key")
        pkey = None
        try:
            if key_file and key_file.filename:
                raw = key_file.read(MAX_KEY_BYTES + 1)
                if len(raw) > MAX_KEY_BYTES:
                    raise SourceError("The private key file is too large.")
                pkey = load_private_key(
                    raw.decode("utf-8", "replace"),
                    request.form.get("ssh_key_passphrase", "") or None,
                )
                del raw
            fetcher = app.config["REMOTE_FACTORY"](
                form["ssh_host"],
                port=form["ssh_port"],
                username=form["ssh_username"],
                password=password or None,
                pkey=pkey,
                accept_unknown_host=form["accept_unknown_host"],
                transfer=form["transfer"],
            )
            del password
            remote = fetcher.fetch_latest(
                form["remote_dir"], form["remote_pattern"], archive,
                max_bytes=app.config["MAX_DOWNLOAD_BYTES"],
            )
        except RemoteError as exc:
            raise SourceError(str(exc), 502) from None
        return f"Linux server {fetcher.host}: {remote.path}"

    def _from_panorama(form: dict, dest: Path) -> tuple[str, dict[str, str], list[str]]:
        password = request.form.get("password", "")
        if not form["host"].strip() or not form["username"].strip() or not password:
            raise SourceError("Panorama host, username and password are required.")
        direct = form["method"] == METHOD_DIRECT
        fw_password = request.form.get("fw_password", "")
        fw_username = form["fw_username"].strip()
        if direct and not (fw_username and fw_password):
            # Default to the Panorama credentials when firewall ones are left blank.
            fw_username, fw_password = form["username"].strip(), password
        factory = app.config["CLIENT_FACTORY"]
        try:
            pano = factory(form["host"], verify=form["verify_ssl"])
            pano.login(form["username"].strip(), password)
            del password
            result = collect_configs(
                pano, dest,
                method=form["method"],
                patterns=parse_filters(form["device_filter"]),
                include_panorama=form["include_panorama"],
                fw_username=fw_username if direct else None,
                fw_password=fw_password if direct else None,
                client_factory=factory if direct else None,
                verify=form["verify_ssl"],
                max_workers=app.config["COLLECT_WORKERS"],
            )
        except PanoramaError as exc:
            raise SourceError(str(exc), 502) from None
        if not result.collected and not result.panorama_collected:
            if not result.devices:
                raise SourceError("No connected devices matched the device filter.", 400)
            raise SourceError(
                "No configurations could be collected. " + " | ".join(result.errors[:5]), 502
            )
        how = "via Panorama proxy" if not direct else "direct to firewalls"
        source = (
            f"Panorama {pano.host} - {len(result.collected)} of {len(result.devices)} "
            f"connected device(s) collected {how}"
        )
        return source, result.serial_map, result.errors

    # -------------------------------------------------------------- routes
    @app.get("/")
    def index():
        return render_template("index.html", form=_form_defaults())

    @app.post("/search")
    def search():
        form = _form_defaults()

        def fail(message: str, status: int = 400):
            flash(message, "error")
            return render_template("index.html", form=form), status

        try:
            terms = parse_terms(form["terms"])
            context = parse_context(form["context"])
            matchers = build_matchers(
                terms, regex=form["regex"], case_sensitive=form["case_sensitive"]
            )
        except ValueError as exc:
            return fail(str(exc))

        with tempfile.TemporaryDirectory(prefix="bundlesearch-") as tmp:
            tmpdir = Path(tmp)
            archive = tmpdir / "bundle.tgz"
            root = tmpdir / "extracted"
            serial_map: dict[str, str] = {}
            errors: list[str] = []
            try:
                if form["mode"] == "panorama":
                    source, serial_map, errors = _from_panorama(form, root)
                else:
                    if form["mode"] == "linux":
                        source = _from_linux(form, archive)
                    else:
                        source = _from_upload(archive)
                    extract_bundle(archive, root, max_total_bytes=app.config["MAX_EXTRACT_BYTES"])
                    archive.unlink(missing_ok=True)
                report = run_search(
                    root, matchers,
                    context=context,
                    regex=form["regex"],
                    case_sensitive=form["case_sensitive"],
                    serial_map=serial_map,
                    source=source,
                )
                report.errors = errors
            except SourceError as exc:
                return fail(str(exc), exc.status)
            except BundleError as exc:
                return fail(str(exc))

        token = store.put(report)
        return render_template("results.html", report=report, matchers=matchers, token=token)

    @app.get("/export/<token>/<fmt>")
    def export(token: str, fmt: str):
        report = store.get(token)
        if report is None:
            flash("These results have expired. Run the search again.", "error")
            return redirect(url_for("index"))
        if fmt not in ("csv", "txt"):
            abort(404)
        stamp = report.created.strftime("%Y%m%d-%H%M%S")
        if fmt == "csv":
            body, mime = "\ufeff" + to_csv(report), "text/csv"  # BOM so Excel detects UTF-8
        else:
            body, mime = to_text(report), "text/plain"
        return Response(
            body,
            mimetype=mime,
            headers={
                "Content-Disposition": f'attachment; filename="bundle-search-{stamp}.{fmt}"'
            },
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Panorama config search web UI")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    # Do not log request bodies; werkzeug only logs method + path (no credentials in URLs).
    create_app().run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
>>>>>>> dev

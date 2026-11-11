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

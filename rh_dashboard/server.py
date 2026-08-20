"""
A small HTTP front end so the dashboard can run somewhere without a shell.

The deployment this exists for is a homelab pod with the statement CSVs on a
persistent volume: there is nowhere to run `./rh-dashboard build`, and no way
to copy a new monthly export into `input/` by hand. So this serves the page and
accepts uploads into the same folder the CLI reads.

**This is `http.server`, not a hardened web server**, and that is a deliberate
trade for the project's standard-library-only rule — adding Flask would mean a
`requirements.txt`, a venv, and a pip layer in the image for four routes. It is
appropriate for one user on a private network behind an ingress, and not for
exposure to the internet. What it does do: cap the request body before reading
it, escape everything it echoes back, refuse any upload the real parser can't
read, keep uploads inside the input folder by construction, and support
optional Basic auth.

Everything user-supplied leaves this module as JSON, never as interpolated
HTML — the one page it serves is built by `dashboard.py` from parsed
`Transaction`s, not from anything a request body carried.
"""
from __future__ import annotations

import hmac
import html
import json
import os
import shutil
import tempfile
import threading
from base64 import b64decode
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import HTTP
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .loader import LoadError, load_file
from .pipeline import build_dashboard

DEFAULT_MAX_UPLOAD = 10 * 1024 * 1024      # 10 MB; a 556-row export is ~50 KB
SAFE_NAME = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
_TRUTHY = ("1", "true", "yes", "on")


class AuthConfigError(RuntimeError):
    """Auth was demanded but not usable. Raised instead of starting."""


@dataclass
class ServerConfig:
    input_dir: Path
    output_dir: Path
    filename: str = "dashboard.html"
    max_upload: int = DEFAULT_MAX_UPLOAD
    username: str | None = None
    password: str | None = None

    @property
    def auth_required(self) -> bool:
        return bool(self.username and self.password)

    @classmethod
    def from_env(cls, input_dir, output_dir, filename: str = "dashboard.html"):
        """
        Credentials come from the environment so the chart can wire them
        straight from a Secret. Auth is off unless *both* are set: a username
        with no password is not "nearly protected", it is open.

        `RH_DASHBOARD_AUTH_REQUIRED` is how the operator says they meant to
        have auth. With it set, missing or empty credentials raise instead of
        starting, so the pod crash-loops visibly rather than serving an
        account statement to anyone who can reach it. That matters most when
        the values arrive from a SOPS-encrypted Secret, where a renamed key or
        an empty string is a plausible mistake and the fail-open version of it
        is silent.
        """
        username = os.environ.get("RH_DASHBOARD_USER") or None
        password = os.environ.get("RH_DASHBOARD_PASSWORD") or None
        required = os.environ.get("RH_DASHBOARD_AUTH_REQUIRED", "").strip().lower() in _TRUTHY
        if required and not (username and password):
            missing = [name for name, val in
                       (("RH_DASHBOARD_USER", username),
                        ("RH_DASHBOARD_PASSWORD", password)) if not val]
            raise AuthConfigError(
                "RH_DASHBOARD_AUTH_REQUIRED is set, but "
                f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} "
                "empty or missing. Refusing to start unauthenticated — check the "
                "Secret's key names and that its values are non-empty.")
        return cls(input_dir=Path(input_dir), output_dir=Path(output_dir),
                   filename=filename,
                   max_upload=int(os.environ.get("RH_DASHBOARD_MAX_UPLOAD",
                                                 DEFAULT_MAX_UPLOAD)),
                   username=username, password=password)


@dataclass
class PageCache:
    """Rebuilds only when the input folder actually changed.

    Keyed on (name, mtime_ns, size) of every CSV rather than on a dirty flag,
    so a file written onto the volume by any other means — kubectl cp, a
    restored backup — is picked up too.
    """
    lock: threading.Lock = field(default_factory=threading.Lock)
    key: tuple | None = None
    html: str | None = None

    @staticmethod
    def _key(input_dir: Path) -> tuple:
        try:
            entries = sorted(input_dir.glob("*.csv"))
        except OSError:
            return ()
        out = []
        for p in entries:
            try:
                st = p.stat()
            except OSError:
                continue
            out.append((p.name, st.st_mtime_ns, st.st_size))
        return tuple(out)

    def get(self, cfg: ServerConfig) -> str:
        with self.lock:
            key = self._key(cfg.input_dir)
            if key != self.key or self.html is None:
                res = build_dashboard(input_dir=cfg.input_dir,
                                      output_dir=cfg.output_dir,
                                      filename=cfg.filename,
                                      interactive=True)
                self.html = Path(res["output"]).read_text(encoding="utf-8")
                self.key = key
            return self.html

    def invalidate(self) -> None:
        with self.lock:
            self.key = None


def safe_name(raw: str) -> str | None:
    """
    Reduce an uploaded filename to something that cannot escape the input
    folder, or return None if nothing usable is left.

    Only the basename survives, and only from `SAFE_NAME`. A leading dot is
    stripped so an upload can't become `.gitignore` or a dotfile the folder
    listing hides.
    """
    base = raw.replace("\\", "/").split("/")[-1].strip()
    cleaned = "".join(ch for ch in base if ch in SAFE_NAME).lstrip(".")
    if not cleaned or not cleaned.lower().endswith(".csv"):
        return None
    return cleaned


def unique_path(input_dir: Path, name: str) -> Path:
    """`statement.csv` -> `statement-2.csv` -> `statement-3.csv`. Never
    overwrites: two files that differ are two statements, and the caller has
    already established the bytes aren't identical."""
    candidate = input_dir / name
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    n = 2
    while (input_dir / f"{stem}-{n}{suffix}").exists():
        n += 1
    return input_dir / f"{stem}-{n}{suffix}"


def parse_multipart(content_type: str, body: bytes) -> list[tuple[str, bytes]]:
    """
    Pull (filename, content) out of a multipart/form-data body.

    `cgi.FieldStorage` was the obvious tool for this and was removed from the
    standard library in Python 3.13, so this uses the documented replacement:
    hand the raw body to the email parser with a synthesised header block.
    """
    raw = b"Content-Type: " + content_type.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n"
    msg = BytesParser(policy=HTTP).parsebytes(raw + body)
    if not msg.is_multipart():
        return []
    out = []
    for part in msg.iter_parts():
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        out.append((filename, payload if payload is not None else b""))
    return out


def validate_csv(data: bytes) -> tuple[bool, str, int]:
    """
    Accept an upload only if the real loader can read it.

    Returns (ok, detail, row_count). This is the "skip loudly, never
    substitute" rule pointed at the front door: rather than guessing whether
    something is a Robinhood export from its name or its header line, run
    `loader.load_file` over it and report what that actually said. A file that
    parses to zero transactions is a rejection, not an empty success.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="rh-upload-"))
    try:
        tmp = tmp_dir / "upload.csv"
        tmp.write_bytes(data)
        try:
            txns, row_errors = load_file(tmp)
        except LoadError as e:
            return False, str(e), 0
        except Exception as e:                      # noqa: BLE001 - report, never 500
            return False, f"could not be parsed: {e}", 0
        if not txns:
            detail = "no transactions found"
            if row_errors:
                detail += f"; first problem: {row_errors[0].split(' — ', 1)[-1]}"
            return False, detail, 0
        detail = f"{len(txns)} transaction(s)"
        if row_errors:
            detail += f", {len(row_errors)} unreadable row(s) skipped"
        return True, detail, len(txns)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _no_data_page(cfg: ServerConfig) -> str:
    """
    The first thing a fresh deployment shows: an empty volume is the normal
    starting state, not an error. `build_dashboard` raises `LoadError` there,
    so serve a page carrying the same upload dialog rather than a 500 the user
    has no way out of.
    """
    from .dashboard import (CSS, INTERACTIVE_CSS, INTERACTIVE_JS, _files_dialog,
                            _header_actions)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Robinhood Portfolio Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{CSS}{INTERACTIVE_CSS}</style>
</head>
<body>
<div class="page">
  <header>{_header_actions()}
    <h1>Robinhood Portfolio Dashboard</h1>
    <p class="meta">No statement files yet</p>
  </header>
  <section class="card">
    <h2>Nothing to show yet</h2>
    <p class="sub-head">Add a Robinhood statement export and the dashboard builds itself.</p>
    <p class="verdict">Use <strong>Statements</strong> at the top right to upload a
      <code>*.csv</code> export, or copy files into
      <code>{html.escape(str(cfg.input_dir))}</code> directly. Monthly exports overlap
      in date range &mdash; add them all, duplicate rows are removed automatically.</p>
  </section>
</div>{_files_dialog()}
<script>{INTERACTIVE_JS}</script>
</body>
</html>
"""


def make_handler(cfg: ServerConfig, cache: PageCache | None = None):
    cache = cache or PageCache()

    class Handler(BaseHTTPRequestHandler):
        server_version = "rh-dashboard"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        # -- plumbing ----------------------------------------------------
        def _send(self, status, body: bytes, content_type: str, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # The page embeds its own CSS/JS and must never pull anything
            # external; say so in a header as well as by construction.
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; img-src 'self' data:; "
                             "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                             "connect-src 'self'; form-action 'self'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status, payload: dict):
            self._send(status, json.dumps(payload).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _html(self, status, markup: str):
            self._send(status, markup.encode("utf-8"), "text/html; charset=utf-8")

        def _authorized(self) -> bool:
            if not cfg.auth_required:
                return True
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return False
            try:
                user, _, pw = b64decode(header[6:]).decode("utf-8").partition(":")
            except Exception:                       # noqa: BLE001 - malformed header
                return False
            # Both compared, and both always compared, so a valid username
            # doesn't answer faster than an invalid one.
            ok_user = hmac.compare_digest(user, cfg.username or "")
            ok_pw = hmac.compare_digest(pw, cfg.password or "")
            return ok_user and ok_pw

        def _challenge(self):
            self._send(HTTPStatus.UNAUTHORIZED, b'{"detail": "authentication required"}',
                       "application/json; charset=utf-8",
                       {"WWW-Authenticate": 'Basic realm="rh-dashboard"'})

        def _read_body(self) -> bytes | None:
            """Returns None when the body is missing or over the cap. The
            length is checked before a single byte is read."""
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return None
            if length <= 0 or length > cfg.max_upload:
                return None
            return self.rfile.read(length)

        def log_message(self, fmt, *args):
            # One line per request, without the client's arbitrary request
            # line echoed raw into the log.
            print(f"  {self.command} {self.path.split('?')[0]} -> {args[1] if len(args) > 1 else ''}")

        # -- routes ------------------------------------------------------
        def do_GET(self):                            # noqa: N802 - stdlib name
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/healthz":
                # Auth-exempt and touches no disk: a probe must not fail
                # because the volume is slow or the credentials rotated.
                self._json(HTTPStatus.OK, {"status": "ok"})
                return
            if not self._authorized():
                self._challenge()
                return
            if path == "/":
                try:
                    self._html(HTTPStatus.OK, cache.get(cfg))
                except LoadError:
                    self._html(HTTPStatus.OK, _no_data_page(cfg))
                return
            if path == "/api/files":
                self._json(HTTPStatus.OK, {"files": self._list_files()})
                return
            self._json(HTTPStatus.NOT_FOUND, {"detail": "not found"})

        do_HEAD = do_GET

        def do_POST(self):                           # noqa: N802 - stdlib name
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if not self._authorized():
                self._challenge()
                return
            if path == "/api/upload":
                self._upload()
                return
            if path == "/api/files/delete":
                self._delete()
                return
            self._json(HTTPStatus.NOT_FOUND, {"detail": "not found"})

        # -- handlers ----------------------------------------------------
        def _list_files(self) -> list[dict]:
            out = []
            for p in sorted(cfg.input_dir.glob("*.csv")):
                try:
                    txns, _ = load_file(p)
                    rows = len(txns)
                except Exception:                    # noqa: BLE001 - listing is best-effort
                    rows = 0
                out.append({"name": p.name, "bytes": p.stat().st_size, "rows": rows})
            return out

        def _upload(self):
            ctype = self.headers.get("Content-Type", "")
            if not ctype.startswith("multipart/form-data"):
                self._json(HTTPStatus.BAD_REQUEST,
                           {"status": "rejected", "detail": "expected a file upload"})
                return
            body = self._read_body()
            if body is None:
                cap = (f"{cfg.max_upload / (1024 * 1024):.0f} MB"
                       if cfg.max_upload >= 1024 * 1024 else f"{cfg.max_upload} bytes")
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                           {"status": "rejected",
                            "detail": f"empty, or larger than the {cap} limit"})
                return
            try:
                parts = parse_multipart(ctype, body)
            except Exception as e:                   # noqa: BLE001 - malformed body
                self._json(HTTPStatus.BAD_REQUEST,
                           {"status": "rejected", "detail": f"malformed upload: {e}"})
                return
            if not parts:
                self._json(HTTPStatus.BAD_REQUEST,
                           {"status": "rejected", "detail": "no file in the request"})
                return

            raw_name, data = parts[0]
            name = safe_name(raw_name)
            if name is None:
                self._json(HTTPStatus.BAD_REQUEST,
                           {"status": "rejected",
                            "detail": "only *.csv statement exports are accepted"})
                return

            ok, detail, rows = validate_csv(data)
            if not ok:
                self._json(HTTPStatus.BAD_REQUEST,
                           {"status": "rejected", "detail": detail})
                return

            # Identical bytes already on the volume is a no-op, not an error:
            # re-uploading last month's export is the normal way to find out
            # whether it was already added.
            existing = cfg.input_dir / name
            if existing.exists() and existing.read_bytes() == data:
                self._json(HTTPStatus.OK,
                           {"status": "duplicate", "saved_as": name,
                            "detail": "already uploaded, unchanged"})
                return

            cfg.input_dir.mkdir(parents=True, exist_ok=True)
            target = unique_path(cfg.input_dir, name)
            target.write_bytes(data)
            cache.invalidate()
            self._json(HTTPStatus.OK,
                       {"status": "saved", "saved_as": target.name,
                        "rows": rows, "detail": detail})

        def _delete(self):
            body = self._read_body()
            try:
                name = safe_name(json.loads(body or b"{}").get("name", ""))
            except (ValueError, AttributeError):
                name = None
            if name is None:
                self._json(HTTPStatus.BAD_REQUEST,
                           {"status": "rejected", "detail": "no such file"})
                return
            target = cfg.input_dir / name
            if not target.is_file():
                self._json(HTTPStatus.NOT_FOUND,
                           {"status": "rejected", "detail": "no such file"})
                return
            target.unlink()
            cache.invalidate()
            self._json(HTTPStatus.OK, {"status": "deleted", "name": name})

    return Handler


def serve(host: str, port: int, cfg: ServerConfig) -> int:
    cfg.input_dir.mkdir(parents=True, exist_ok=True)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((host, port), make_handler(cfg))
    bound_host, bound_port = httpd.server_address[:2]
    print(f"  rh-dashboard serving on http://{bound_host}:{bound_port}")
    print(f"  input       {cfg.input_dir}")
    print(f"  output      {cfg.output_dir}")
    print(f"  auth        {'basic (RH_DASHBOARD_USER)' if cfg.auth_required else 'none'}")
    print("  Ctrl-C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
    finally:
        httpd.server_close()
    return 0

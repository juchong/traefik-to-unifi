"""Optional read-only web UI for traefik-to-unifi.

Serves the DNS state ledger, sync history, and a tail of the log file over
plain HTTP using only the Python standard library (no new dependencies).

GET-only: every mutating method returns 405. All data is read from files in the
``/data`` volume, so viewing needs no Docker socket / root (unlike ``docker
logs``). Files are re-read per request; missing files render as empty sections.
"""

import html
import json
import logging
import os
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- Pure helpers (no sockets/threads -> unit-testable) ---


def tail(path, n):
    """Return the last ``n`` lines of a text file as a list; missing -> [].

    ponytail: reads the whole file. The log is capped by RotatingFileHandler
    (default 1 MB) and history by SYNC_HISTORY_MAX, so this stays cheap.
    Upgrade to a seek-from-end reader only if those caps grow large.
    """
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, errors="replace") as f:
            return f.read().splitlines()[-n:]
    except OSError:
        return []


def load_state(path):
    """Load the two-bucket ledger; missing/corrupt -> empty buckets."""
    try:
        with open(path) as f:
            data = json.load(f)
        return {
            "managed": data.get("managed") or {},
            "unmanaged": data.get("unmanaged") or {},
        }
    except (OSError, ValueError):
        return {"managed": {}, "unmanaged": {}}


def load_history(path, n):
    """Load the last ``n`` sync-history JSON-line records (oldest first)."""
    out = []
    for line in tail(path, n):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _cell(value):
    return html.escape(str(value))


def render_page(state, history, log_lines):
    """Render the full self-contained HTML page. Pure -> unit-testable."""
    managed = state.get("managed", {})
    unmanaged = state.get("unmanaged", {})

    managed_rows = (
        "".join(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                _cell(h),
                _cell(m.get("value", "")),
                _cell(m.get("record_type", "")),
                "yes" if m.get("prune_eligible") else "no",
                _cell(m.get("missing_count", 0)),
            )
            for h, m in sorted(managed.items())
        )
        or '<tr><td colspan="5" class="empty">none</td></tr>'
    )

    unmanaged_rows = (
        "".join(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                _cell(h),
                _cell(u.get("value", "")),
                _cell(u.get("record_type", "")),
                _cell(u.get("reason", "")),
                _cell(u.get("first_seen", "")),
            )
            for h, u in sorted(unmanaged.items())
        )
        or '<tr><td colspan="5" class="empty">none</td></tr>'
    )

    history_rows = (
        "".join(
            "<tr class='{}'><td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
            "<td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                "err" if r.get("status") == "error" else "",
                _cell(r.get("ts", "")),
                _cell(r.get("status", "")),
                _cell(r.get("added", 0)),
                _cell(r.get("updated", 0)),
                _cell(r.get("deleted", 0)),
                _cell(r.get("released", 0)),
                _cell(r.get("errors", 0)),
                "yes" if r.get("dry_run") else "no",
            )
            for r in reversed(history)
        )
        or '<tr><td colspan="9" class="empty">none</td></tr>'
    )

    log_text = _cell("\n".join(log_lines)) or "(empty)"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>traefik-to-unifi DNS</title>
<style>
 body{{font:14px/1.4 system-ui,sans-serif;margin:1.5rem;color:#1a1a1a;background:#fafafa}}
 h1{{font-size:1.3rem}} h2{{font-size:1rem;margin-top:1.5rem}}
 table{{border-collapse:collapse;width:100%;background:#fff;margin-top:.4rem}}
 th,td{{border:1px solid #ddd;padding:.35rem .5rem;text-align:left;font-size:13px}}
 th{{background:#f0f0f0}}
 tr.err{{background:#fdecec}}
 td.empty{{color:#888;font-style:italic}}
 pre{{background:#111;color:#ddd;padding:.75rem;overflow:auto;max-height:22rem;font-size:12px}}
 .muted{{color:#666;font-size:12px}}
</style>
</head>
<body>
<h1>traefik-to-unifi DNS map</h1>
<p class="muted">Read-only. Auto-refreshes every 30s. Managed = created by this tool
(prune-eligible). Unmanaged = manual/orphan/released entries (never auto-deleted).</p>

<h2>Managed ({len(managed)})</h2>
<table><thead><tr><th>Host</th><th>Value</th><th>Type</th>
<th>Prune eligible</th><th>Missing count</th></tr></thead>
<tbody>{managed_rows}</tbody></table>

<h2>Unmanaged / orphan ({len(unmanaged)})</h2>
<table><thead><tr><th>Host</th><th>Value</th><th>Type</th>
<th>Reason</th><th>First seen</th></tr></thead>
<tbody>{unmanaged_rows}</tbody></table>

<h2>Sync history (newest first)</h2>
<table><thead><tr><th>Timestamp</th><th>Status</th><th>Added</th><th>Updated</th>
<th>Deleted</th><th>Released</th><th>Errors</th><th>Dry run</th></tr></thead>
<tbody>{history_rows}</tbody></table>

<h2>Log tail</h2>
<pre>{log_text}</pre>
</body>
</html>
"""


def _liveness(cfg):
    """Readiness based on the last sync-history timestamp."""
    history = load_history(cfg["history_file"], 1)
    if not history:
        return True, "starting"
    try:
        last = datetime.fromisoformat(history[-1].get("ts"))
        age = (datetime.now(UTC) - last).total_seconds()
    except (TypeError, ValueError):
        return True, "unknown-ts"
    if age <= cfg["liveness_max_age"]:
        return True, f"ok (last sync {int(age)}s ago)"
    return False, f"stale (last sync {int(age)}s ago)"


class _Handler(BaseHTTPRequestHandler):
    config = {}  # overridden per-server via a subclass

    def _send(self, code, body, content_type="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def log_message(self, *args):
        pass  # keep the application log clean; no access logging needed

    def do_GET(self):  # noqa: N802 (name mandated by http.server)
        cfg = self.config
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/":
            self._send(
                200,
                render_page(
                    load_state(cfg["state_file"]),
                    load_history(cfg["history_file"], cfg["history_max"]),
                    tail(cfg["log_file"], cfg["log_tail"]),
                ),
            )
        elif path == "/api/state":
            self._send(
                200,
                json.dumps(load_state(cfg["state_file"]), indent=2),
                "application/json",
            )
        elif path == "/api/history":
            self._send(
                200,
                json.dumps(
                    load_history(cfg["history_file"], cfg["history_max"]), indent=2
                ),
                "application/json",
            )
        elif path == "/api/log":
            self._send(
                200,
                "\n".join(tail(cfg["log_file"], cfg["log_tail"])),
                "text/plain; charset=utf-8",
            )
        elif path == "/healthz":
            ok, msg = _liveness(cfg)
            self._send(200 if ok else 503, msg, "text/plain; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    do_HEAD = do_GET  # noqa: N815 (name mandated by http.server)

    def _reject(self):
        self._send(405, "read-only", "text/plain; charset=utf-8")

    # Names mandated by http.server; all mutating verbs are rejected.
    do_POST = do_PUT = do_DELETE = do_PATCH = _reject  # noqa: N815


def start_web_ui():
    """Start the read-only UI in a daemon thread when WEB_UI_ENABLED.

    Best-effort: a bind failure logs an error and returns None so the sync loop
    keeps running (the UI is non-essential). Returns the server or None.
    """
    if os.environ.get("WEB_UI_ENABLED", "false") not in ("1", "true", "True", "TRUE"):
        return None

    cfg = {
        "state_file": os.environ.get("DNS_STATE_FILE", "/data/dns-state.json"),
        "history_file": os.environ.get("SYNC_HISTORY_FILE", "/data/sync-history.jsonl"),
        "history_max": int(os.environ.get("SYNC_HISTORY_MAX", "200")),
        "log_file": os.environ.get("LOG_FILE", "/data/traefik-to-unifi.log"),
        "log_tail": int(os.environ.get("WEB_UI_LOG_TAIL", "200")),
        "liveness_max_age": int(os.environ.get("WEB_UI_LIVENESS_MAX_AGE", "300")),
    }
    bind = os.environ.get("WEB_UI_BIND", "0.0.0.0")
    port = int(os.environ.get("WEB_UI_PORT", "8080"))

    handler = type("_ConfiguredHandler", (_Handler,), {"config": cfg})
    try:
        server = ThreadingHTTPServer((bind, port), handler)
    except OSError as e:
        logging.error(f"Web UI disabled: cannot bind {bind}:{port}: {e}")
        return None

    threading.Thread(target=server.serve_forever, name="webui", daemon=True).start()
    logging.info(f"Read-only web UI listening on http://{bind}:{port}")
    return server

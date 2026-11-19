"""HTTP API (manual trigger, plan, history) and the cron scheduler loop."""

from __future__ import annotations

import base64
import hmac
import html
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from . import __version__, logs
from .coolify import CoolifyError
from .engine import Engine, RunInProgress, describe

MAX_BODY = 64 * 1024


class Health:
    """Background check of Coolify reachability + token validity (GET /api/v1/version)."""

    def __init__(self, engine: Engine, interval: int = 60):
        self.engine = engine
        self.interval = interval
        self.ok = False
        self.detail = "not checked yet"
        self.coolify_version = None
        self.checked_at = 0.0

    def check(self) -> None:
        r = self.engine.coolify.version()
        self.checked_at = time.time()
        if r.ok:
            was_ok, self.ok = self.ok, True
            self.coolify_version = r.body.decode(errors="replace").strip().strip('"')
            self.detail = "ok"
            if not was_ok:
                logs.get().info("coolify reachable", fields={"coolify_version": self.coolify_version})
        else:
            previous = self.detail
            self.ok = False
            self.detail = f"GET /api/v1/version: HTTP {r.status or '000'} {r.text(120)}"
            if self.detail != previous:  # log changes only, not every minute
                logs.get().warning("coolify check failed", fields={"detail": self.detail})

    def loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.check()
            except Exception as e:
                self.ok, self.detail = False, str(e)
            stop.wait(self.interval)


class Scheduler:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.cfg = engine.cfg
        self.tz = ZoneInfo(self.cfg.timezone)
        self.next_run: datetime | None = None

    def loop(self, stop: threading.Event) -> None:
        log = logs.get()
        if not self.cfg.cron:
            log.info("schedule disabled (CRON_SCHEDULE=off): runs only on demand")
            return
        while not stop.is_set():
            self.next_run = self.cfg.cron.next_after(datetime.now(timezone.utc), self.tz)
            log.info("next scheduled run", fields={
                "at": self.next_run.astimezone(self.tz).isoformat(timespec="minutes"),
                "schedule": self.cfg.cron_raw})
            while not stop.is_set():
                delay = (self.next_run - datetime.now(timezone.utc)).total_seconds()
                if delay <= 0:
                    break
                stop.wait(min(delay, 30))
            if stop.is_set():
                return
            try:
                self.engine.run("schedule")
            except RunInProgress as e:
                log.warning("scheduled run skipped", fields={"reason": str(e)})
            except Exception:
                log.exception("scheduled run crashed")
            time.sleep(1)  # never fire twice within the same minute


def _iso(ts) -> str | None:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds") if ts else None


def make_handler(engine: Engine, health: Health, scheduler: Scheduler):
    cfg = engine.cfg
    history = engine.history

    class Handler(BaseHTTPRequestHandler):
        server_version = f"coolify-redeployer/{__version__}"
        sys_version = ""

        def log_message(self, fmt, *args):  # route access logs through our logger
            logs.get().debug("http", fields={"client": self.client_address[0], "request": fmt % args})

        # --- helpers ---------------------------------------------------------------------

        def _send(self, status: int, payload, content_type="application/json", extra_headers=None):
            body = (json.dumps(payload, indent=2, default=str) + "\n").encode() \
                if content_type == "application/json" else payload.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _authorized(self) -> bool:
            if not cfg.api_enabled:
                return False
            auth = self.headers.get("Authorization", "")
            supplied = ""
            if auth.startswith("Bearer "):
                supplied = auth[7:].strip()
            elif auth.startswith("Basic "):
                try:
                    supplied = base64.b64decode(auth[6:]).decode().partition(":")[2]
                except Exception:
                    supplied = ""
            return bool(supplied) and hmac.compare_digest(supplied.encode(), cfg.trigger_token.encode())

        def _require_auth(self) -> bool:
            if not cfg.api_enabled:
                self._send(404, {"error": "API disabled: set TRIGGER_TOKEN to enable it"})
                return False
            if not self._authorized():
                logs.get().warning("unauthorized API request", fields={
                    "client": self.headers.get("X-Forwarded-For", self.client_address[0]), "path": self.path})
                self._send(401, {"error": "unauthorized"},
                           extra_headers={"WWW-Authenticate": 'Basic realm="coolify-redeployer"'})
                return False
            return True

        def _wants_html(self, query) -> bool:
            fmt = (query.get("format") or [""])[0]
            return fmt == "html" or (not fmt and "text/html" in self.headers.get("Accept", ""))

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("request body too large")
            raw = self.rfile.read(length) if length else b""
            if not raw.strip():
                return {}
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("body must be a JSON object")
            return data

        # --- routes ----------------------------------------------------------------------

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            path = url.path.rstrip("/") or "/"
            if path == "/healthz":
                status = 200 if health.ok else 503
                return self._send(status, {"ok": health.ok, "detail": health.detail,
                                           "coolify_version": health.coolify_version,
                                           "checked_at": _iso(health.checked_at)})
            if not self._require_auth():
                return
            limit = max(1, min(int((q.get("limit") or ["50"])[0] or 50), 1000))
            if path in ("/", "/status"):
                return self._send(200, {
                    "version": __version__, "busy": engine.busy, "current_run": engine.current_run,
                    "schedule": cfg.cron_raw if cfg.cron else "off", "timezone": cfg.timezone,
                    "next_run": scheduler.next_run.astimezone(scheduler.tz).isoformat(timespec="minutes")
                    if scheduler.next_run else None,
                    "coolify": {"ok": health.ok, "version": health.coolify_version, "detail": health.detail},
                    "alerts": [n for n, on in (("ntfy", cfg.ntfy), ("novu", cfg.novu)) if on],
                    "last_runs": [_run_view(r) for r in history.runs(5)],
                })
            if path == "/plan":
                try:
                    plan = engine.plan()
                except CoolifyError as e:
                    return self._send(502, {"error": str(e)})
                return self._send(200, {"targets": [r.to_dict() for r in plan.targets],
                                        "excluded": plan.excluded, "warnings": plan.warnings,
                                        "total": plan.total})
            if path == "/runs":
                return self._send(200, [_run_view(r) for r in history.runs(limit)])
            if path.startswith("/runs/"):
                parts = path.split("/")
                if len(parts) >= 3 and parts[2].isdigit():
                    run_id = int(parts[2])
                    if len(parts) == 4 and parts[3] == "logs":
                        lines = history.logs(run_id)
                        if (q.get("format") or [""])[0] == "text":
                            text = "\n".join(f"{_iso(l['at'])} {l['level'].upper():<7} {l['message']} "
                                             + " ".join(f"{k}={v}" for k, v in l["fields"].items())
                                             for l in lines)
                            return self._send(200, text + "\n", "text/plain")
                        return self._send(200, [l | {"at": _iso(l["at"])} for l in lines])
                    run = history.run(run_id)
                    if not run:
                        return self._send(404, {"error": "run not found"})
                    return self._send(200, _run_view(run) | {"changes": [_change_view(c) for c in run["changes"]]})
            if path == "/history":
                rows = history.changes(resource=(q.get("resource") or [None])[0],
                                       change=(q.get("change") or [None])[0], limit=limit)
                if self._wants_html(q):
                    return self._send(200, _history_html(rows), "text/html")
                return self._send(200, [_change_view(c) for c in rows])
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            path = urlparse(self.path).path.rstrip("/")
            if path not in ("/trigger", "/deploy"):
                return self._send(404, {"error": "not found"})
            if not self._require_auth():
                return
            try:
                body = self._body()
            except (ValueError, json.JSONDecodeError) as e:
                return self._send(400, {"error": f"invalid JSON body: {e}"})
            only = body.get("uuids")
            if only is not None and (not isinstance(only, list) or not all(isinstance(u, str) and u.isalnum()
                                                                          for u in only)):
                return self._send(400, {"error": "uuids must be a list of resource UUIDs"})
            dry_run = bool(body.get("dry_run", False))
            wait = bool(body.get("wait", False))
            client = self.headers.get("X-Forwarded-For", self.client_address[0])
            logs.get().info("manual trigger received", fields={
                "client": client, "dry_run": dry_run, "uuids": ",".join(only) if only else "all"})
            if engine.busy:
                return self._send(409, {"error": "a run is already in progress", "run_id": engine.current_run})
            if wait:
                try:
                    report = engine.run("api", dry_run=dry_run, only=only)
                except RunInProgress as e:
                    return self._send(409, {"error": str(e)})
                return self._send(200, report)
            started = threading.Event()
            box: dict = {}

            def worker():
                try:
                    engine.run("api", dry_run=dry_run, only=only,
                               on_start=lambda rid: (box.__setitem__("run_id", rid), started.set()))
                except RunInProgress as e:
                    box["error"] = str(e)
                except Exception as e:
                    box["error"] = str(e)
                finally:
                    started.set()

            threading.Thread(target=worker, name="api-run", daemon=True).start()
            started.wait(10)
            if "run_id" not in box:
                return self._send(409, {"error": box.get("error", "run did not start")})
            rid = box["run_id"]
            return self._send(202, {"run_id": rid, "status": "started", "dry_run": dry_run,
                                    "links": {"run": f"/runs/{rid}", "logs": f"/runs/{rid}/logs"}})

    return Handler


def _run_view(r: dict) -> dict:
    return {"id": r["id"], "trigger": r["trigger"], "dry_run": bool(r["dry_run"]), "status": r["status"],
            "started_at": _iso(r["started_at"]), "finished_at": _iso(r.get("finished_at")),
            "summary": r.get("summary") or {}}


def _change_view(c: dict) -> dict:
    return {
        "at": _iso(c["at"]), "run_id": c["run_id"], "resource": c["resource_name"],
        "uuid": c["resource_uuid"], "type": c["resource_type"], "project": c["project"],
        "environment": c["environment"], "component": c["component"] or None, "kind": c["kind"],
        "subject": c["subject"], "change": c["change"], "status": c["deploy_status"],
        "from": {"version": c["from_version"], "label": c["from_label"]},
        "to": {"version": c["to_version"], "label": c["to_label"]},
        "path": f"{describe(c['from_version'], c['from_label'])} -> {describe(c['to_version'], c['to_label'])}"
        if c["kind"] != "none" else None,
        "deployment_uuid": c["deployment_uuid"], "http_status": c["http_status"], "message": c["message"],
        **({"trigger": c["trigger"]} if "trigger" in c else {}),
    }


def _history_html(rows: list[dict]) -> str:
    e = html.escape
    trs = []
    for c in rows:
        v = _change_view(c)
        name = e(v["resource"] or "") + (f" / {e(v['component'])}" if v["component"] else "")
        trs.append(
            f"<tr class='{e(v['change'])}'><td>{e(v['at'] or '')}</td><td>#{v['run_id']}</td>"
            f"<td>{name}<br><small>{e(v['project'] or '?')}/{e(v['environment'] or '?')}</small></td>"
            f"<td><code>{e(v['subject'] or '')}</code></td>"
            f"<td>{e(describe(c['from_version'], c['from_label']))}</td>"
            f"<td>{e(describe(c['to_version'], c['to_label']))}</td>"
            f"<td><b>{e(v['change'])}</b><br><small>{e(v['status'] or '')}</small></td>"
            f"<td><small>{e(v['message'] or '')}</small></td></tr>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Redeploy history</title>
<style>
:root{{--bg:#fff;--fg:#1a1a1a;--muted:#666;--line:#e3e3e3;--up:#e7f6ec;--fail:#fdecec}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16181c;--fg:#e8e8e8;--muted:#9a9a9a;--line:#2c2f35;--up:#17301f;--fail:#3a1c1c}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif;margin:16px}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}}
small{{color:var(--muted)}}tr.upgraded{{background:var(--up)}}tr.failed{{background:var(--fail)}}
.wrap{{overflow-x:auto}}code{{word-break:break-all}}
</style></head><body><h1>Redeploy history</h1>
<p><small>Newest first. Filter with ?resource=&lt;uuid or name&gt;, ?change=upgraded, ?limit=N.</small></p>
<div class="wrap"><table><thead><tr><th>When</th><th>Run</th><th>Resource</th><th>Image / repository</th>
<th>From</th><th>To</th><th>Result</th><th>Detail</th></tr></thead><tbody>{''.join(trs)}</tbody></table></div>
</body></html>"""


def serve(engine: Engine) -> None:
    cfg = engine.cfg
    log = logs.get()
    stop = threading.Event()
    health = Health(engine)
    scheduler = Scheduler(engine)
    threading.Thread(target=health.loop, args=(stop,), name="health", daemon=True).start()
    threading.Thread(target=scheduler.loop, args=(stop,), name="scheduler", daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", cfg.http_port), make_handler(engine, health, scheduler))
    httpd.daemon_threads = True
    log.info("http listening", fields={"port": cfg.http_port,
                                       "api": "enabled" if cfg.api_enabled else "disabled (no TRIGGER_TOKEN)"})

    import signal

    def shutdown(signum, _frame):
        log.info("stopping", fields={"signal": signal.Signals(signum).name})
        stop.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        httpd.serve_forever(poll_interval=1)
    finally:
        httpd.server_close()

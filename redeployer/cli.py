"""Command line: `serve` (default), `run`, `plan`, `history`, `runs`, `logs`, `check-config`."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from . import __version__, config, logs
from .engine import Engine, RunInProgress, describe
from .history import History


def _iso(ts) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M") if ts else "-"


def _table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)] if rows else [len(h) for h in headers]
    fmt = "  ".join("{:<%d}" % w for w in widths)
    return "\n".join([fmt.format(*headers), fmt.format(*["-" * w for w in widths])]
                     + [fmt.format(*map(str, r)) for r in rows])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="redeployer", description="Redeploy Coolify resources.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="scheduler + HTTP API (default)")
    r = sub.add_parser("run", help="run a redeploy now")
    r.add_argument("--dry-run", action="store_true", help="show what would happen, deploy nothing")
    r.add_argument("uuids", nargs="*", help="limit to these resource UUIDs (exclusions still apply)")
    sub.add_parser("plan", help="list the resources a run would redeploy, and the excluded ones")
    h = sub.add_parser("history", help="upgrade history, newest first")
    h.add_argument("--resource", help="resource UUID or name")
    h.add_argument("--change", help="upgraded, unchanged, first-seen, unknown, skipped, failed")
    h.add_argument("--limit", type=int, default=50)
    h.add_argument("--json", action="store_true")
    rs = sub.add_parser("runs", help="list recent runs")
    rs.add_argument("--limit", type=int, default=20)
    lg = sub.add_parser("logs", help="print the captured log of a run")
    lg.add_argument("run_id", type=int)
    sub.add_parser("check-config", help="validate the environment and exit")
    args = p.parse_args(argv)
    cmd = args.cmd or "serve"

    try:
        cfg = config.load()
    except config.ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2
    if cmd == "check-config":
        print("configuration ok")
        return 0

    logs.setup(cfg.log_level, cfg.log_format, cfg.secrets)
    history = History(cfg.data_dir)

    if cmd == "history":
        rows = history.changes(resource=args.resource, change=args.change, limit=args.limit)
        if args.json:
            from .server import _change_view
            print(json.dumps([_change_view(c) for c in rows], indent=2, default=str))
            return 0
        print(_table([[_iso(c["at"]), f"#{c['run_id']}",
                       c["resource_name"] + (f"/{c['component']}" if c["component"] else ""),
                       f"{c['project'] or '?'}/{c['environment'] or '?'}", c["subject"] or "-",
                       describe(c["from_version"], c["from_label"]) if c["kind"] != "none" else "-",
                       describe(c["to_version"], c["to_label"]) if c["kind"] != "none" else "-",
                       c["change"], c["deploy_status"] or ""] for c in rows],
                     ["when", "run", "resource", "project/env", "image/repo", "from", "to", "change", "status"]))
        return 0
    if cmd == "runs":
        print(_table([[f"#{r['id']}", _iso(r["started_at"]), r["trigger"] + (" (dry)" if r["dry_run"] else ""),
                       r["status"], " ".join(f"{k}={v}" for k, v in (r["summary"] or {}).items() if v)]
                      for r in history.runs(args.limit)], ["run", "started", "trigger", "status", "summary"]))
        return 0
    if cmd == "logs":
        for line in history.logs(args.run_id):
            extra = " ".join(f"{k}={v}" for k, v in line["fields"].items())
            print(f"{_iso(line['at'])} {line['level'].upper():<7} {line['message']} {extra}".rstrip())
        return 0

    engine = Engine(cfg, history)
    if cmd == "plan":
        plan = engine.plan()
        for w in plan.warnings:
            print(f"warning: {w}", file=sys.stderr)
        print(f"{len(plan.targets)} to redeploy, {len(plan.excluded)} excluded, {plan.total} total\n")
        print(_table([[r.name, r.uuid, r.type, r.location] for r in plan.targets],
                     ["redeploy", "uuid", "type", "project/env"]))
        if plan.excluded:
            print()
            print(_table([[x["name"], x["uuid"], x["type"], f"{x['project'] or '?'}/{x['environment'] or '?'}",
                           x["reason"]] for x in plan.excluded],
                         ["excluded", "uuid", "type", "project/env", "reason"]))
        return 0

    logs.attach_run_capture(logs.logging.getLogger(logs.LOGGER_NAME), history.add_log)
    if cmd == "run":
        try:
            report = engine.run("cli", dry_run=args.dry_run, only=args.uuids or None)
        except RunInProgress as e:
            print(f"not started: {e}", file=sys.stderr)
            return 3
        return 1 if report["counts"]["failed"] else 0

    # serve
    log = logs.get()
    interrupted = history.mark_interrupted()
    if interrupted:
        log.warning("previous runs were interrupted by a restart", fields={"runs": interrupted})
    log.info("coolify-redeployer starting", fields={
        "version": __version__, "coolify": cfg.coolify_url, "schedule": cfg.cron_raw if cfg.cron else "off",
        "tz": cfg.timezone, "mode": "UUIDS allowlist" if cfg.uuids else "all resources",
        "types": ",".join(cfg.resource_types), "exclude": ", ".join(map(str, cfg.exclude)) or "-",
        "alerts": ",".join(n for n, on in (("ntfy", cfg.ntfy), ("novu", cfg.novu)) if on) or "-"})
    if not cfg.self_uuid and not cfg.uuids:
        log.warning("own resource UUID unknown (COOLIFY_RESOURCE_UUID / SELF_UUID not set): "
                    "add this redeployer to EXCLUDE so it does not redeploy itself")
    from .server import serve
    serve(engine)
    return 0

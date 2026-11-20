"""Optional alerts after a run: ntfy (JSON publish) and Novu (workflow trigger)."""

from __future__ import annotations

import base64

from . import http
from .config import Config


def should_notify(cfg: Config, report: dict) -> bool:
    if report.get("dry_run") or "none" in cfg.notify_on:
        return False
    counts = report["counts"]
    return ("always" in cfg.notify_on
            or ("upgrade" in cfg.notify_on and counts.get("upgraded", 0) > 0)
            or ("failure" in cfg.notify_on and counts.get("failed", 0) > 0))


def title(report: dict) -> str:
    c = report["counts"]
    parts = []
    if c.get("upgraded"):
        parts.append(f"{c['upgraded']} upgraded")
    if c.get("failed"):
        parts.append(f"{c['failed']} failed")
    if not parts:
        parts.append(f"{c.get('deployed', 0)} redeployed, no version change")
    return "Coolify redeploy: " + ", ".join(parts)


def body(report: dict) -> str:
    lines = []
    for u in report["upgrades"]:
        lines.append(f"UPGRADED {u['resource']} ({u['location']}) {u['path']}")
    for f in report["failures"]:
        lines.append(f"FAILED {f['resource']} ({f['location']}): {f['message']}")
    c = report["counts"]
    lines.append(f"Run #{report['run_id']} ({report['trigger']}): {c.get('deployed', 0)} redeployed, "
                 f"{c.get('upgraded', 0)} upgraded, {c.get('failed', 0)} failed, "
                 f"{c.get('excluded', 0)} excluded, in {report['duration_s']}s")
    return "\n".join(lines)


def send_ntfy(cfg: Config, report: dict) -> http.Response:
    n = cfg.ntfy
    base, _, topic = n.url.rpartition("/")
    failed = report["counts"].get("failed", 0) > 0
    payload = {
        "topic": topic,
        "title": title(report),
        "message": body(report),
        "priority": n.priority or (4 if failed else 3),
        "tags": ["coolify", "failure" if failed else "upgrade"],
    }
    headers = {}
    if n.token:
        headers["Authorization"] = f"Bearer {n.token}"
    elif n.username:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{n.username}:{n.password}".encode()).decode()
    # JSON publishing goes to the server root, not the topic URL (ntfy docs, "Publish as JSON").
    return http.request("POST", base + "/", headers=headers, json_body=payload, timeout=15)


def send_novu(cfg: Config, report: dict) -> http.Response:
    n = cfg.novu
    payload = {
        "name": n.workflow_id,
        "to": {"subscriberId": n.subscriber_id},
        "payload": {
            "title": title(report),
            "text": body(report),
            "runId": report["run_id"],
            "trigger": report["trigger"],
            "counts": report["counts"],
            "upgrades": report["upgrades"],
            "failures": report["failures"],
        },
    }
    return http.request("POST", f"{n.api_url}/v1/events/trigger",
                        headers={"Authorization": f"ApiKey {n.api_key}"}, json_body=payload, timeout=15)


def dispatch(cfg: Config, report: dict, log) -> None:
    if not should_notify(cfg, report):
        return
    for name, enabled, fn in (("ntfy", cfg.ntfy, send_ntfy), ("novu", cfg.novu, send_novu)):
        if not enabled:
            continue
        r = fn(cfg, report)
        if r.ok:
            log.info(f"alert sent via {name}", fields={"channel": name, "http": r.status})
        else:
            log.warning(f"alert via {name} failed", fields={
                "channel": name, "http": r.status or "000", "response": r.text(200)})

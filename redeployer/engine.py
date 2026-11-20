"""One redeploy run: discover, filter, capture versions, deploy, wait, record, alert."""

from __future__ import annotations

import fcntl
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import logs, notify
from .config import Config
from .coolify import DEPLOYMENT_DONE, Coolify, CoolifyError, Resource
from .history import History
from .registry import Registry, parse_image


class RunInProgress(Exception):
    pass


def short(version: str | None) -> str:
    if not version:
        return "?"
    if version.startswith("sha256:"):
        return version[:19]  # sha256: + 12 hex
    if len(version) >= 40 and all(c in "0123456789abcdef" for c in version.lower()):
        return version[:7]
    return version


def describe(version: str | None, label: str | None) -> str:
    if label and version:
        return f"{label} ({short(version)})"
    return label or short(version)


@dataclass
class Plan:
    targets: list[Resource]
    excluded: list[dict]
    warnings: list[str]
    total: int


@dataclass
class Item:
    """One versioned thing that gets redeployed: an application, or one container of a service."""
    resource: Resource
    component: str = ""
    kind: str = "none"  # image, git, none
    subject: str = ""
    from_version: str | None = None
    to_version: str | None = None
    from_label: str | None = None
    to_label: str | None = None
    deploy_status: str = ""
    deployment_uuid: str | None = None
    http_status: int | None = None
    message: str = ""
    change: str = "unknown"
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.resource.name}/{self.component}" if self.component else self.resource.name

    @property
    def path(self) -> str:
        frm, to = describe(self.from_version, self.from_label), describe(self.to_version, self.to_label)
        return f"{self.subject} {frm} -> {to}".strip()


class Engine:
    def __init__(self, cfg: Config, history: History, coolify: Coolify | None = None,
                 registry: Registry | None = None):
        self.cfg = cfg
        self.history = history
        self.coolify = coolify or Coolify(cfg.coolify_url, cfg.token)
        self.registry = registry or Registry(cfg.registry_credentials)
        self._lock = threading.Lock()
        self.current_run: int | None = None
        self.log = logs.get()

    # --- locking (threads in this process + other processes such as `docker exec ... run`) -----

    def _acquire(self):
        if not self._lock.acquire(blocking=False):
            raise RunInProgress(f"run #{self.current_run} is in progress")
        fh = open(os.path.join(self.cfg.data_dir, "run.lock"), "w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            self._lock.release()
            raise RunInProgress("a run is in progress in another process") from None
        return fh

    def _release(self, fh) -> None:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
        self._lock.release()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    # --- planning ------------------------------------------------------------------------------

    def plan(self, only: list[str] | None = None) -> Plan:
        cfg = self.cfg
        resources = self.coolify.resources()
        warnings: list[str] = []
        needs_map = any(s.needs_project_map for s in cfg.exclude)
        try:
            envs = self.coolify.environment_map()
        except CoolifyError as e:
            if needs_map:
                raise CoolifyError(f"cannot resolve projects/environments needed by EXCLUDE: {e}") from e
            envs = {}
            warnings.append(f"project/environment names unavailable: {e}")
        for r in resources:
            info = envs.get(int(r.environment_id)) if r.environment_id is not None else None
            if info:
                r.project, r.project_uuid = info["project"], info["project_uuid"]
                r.environment, r.environment_uuid = info["environment"], info["environment_uuid"]

        by_uuid = {r.uuid: r for r in resources}
        if cfg.uuids:
            for u in cfg.uuids:
                if u not in by_uuid:
                    warnings.append(f"UUIDS entry {u} not found (deleted, or not in the token's team)")
        if only:
            for u in only:
                if u not in by_uuid:
                    warnings.append(f"requested uuid {u} not found")
        targets, excluded = [], []
        for r in resources:
            reason = None
            if only and r.uuid not in only:
                continue
            if cfg.uuids and r.uuid not in cfg.uuids:
                reason = "not listed in UUIDS"
            elif r.category not in cfg.resource_types:
                reason = f"type {r.type} not in RESOURCE_TYPES"
            elif cfg.self_uuid and r.uuid == cfg.self_uuid:
                reason = "this redeployer itself"
            else:
                for sel in cfg.exclude:
                    if sel.matches(r):
                        reason = f"EXCLUDE {sel}"
                        break
            if reason:
                excluded.append({**r.to_dict(), "reason": reason})
            else:
                targets.append(r)
        targets.sort(key=lambda r: (r.project or "", r.environment or "", r.name.lower()))
        return Plan(targets, excluded, warnings, len(resources))

    # --- run -------------------------------------------------------------------------------------

    def run(self, trigger: str, dry_run: bool = False, only: list[str] | None = None,
            on_start=None) -> dict:
        fh = self._acquire()
        run_id = None
        try:
            run_id = self.history.start_run(trigger, dry_run)
            self.current_run = run_id
            if on_start:
                on_start(run_id)
            return self._run(run_id, trigger, dry_run, only)
        except Exception as e:
            if run_id is not None:
                logs.get(run_id).exception("run crashed", fields={"error": str(e)})
                self.history.finish_run(run_id, "failed", {"error": str(e)})
            raise
        finally:
            self.current_run = None
            self._release(fh)

    def _run(self, run_id: int, trigger: str, dry_run: bool, only: list[str] | None) -> dict:
        log = logs.get(run_id)
        started = time.time()
        log.info("run started", fields={"trigger": trigger, "dry_run": dry_run})
        try:
            plan = self.plan(only)
        except CoolifyError as e:
            log.error("resource discovery failed", fields={"error": str(e)})
            report = self._report(run_id, trigger, dry_run, started, [], [], error=str(e))
            self.history.finish_run(run_id, "failed", report["counts"] | {"error": str(e)})
            notify.dispatch(self.cfg, report | {"counts": report["counts"] | {"failed": 1},
                                                "failures": [{"resource": "discovery", "uuid": "",
                                                              "location": self.cfg.coolify_url,
                                                              "message": str(e)}]}, log)
            return report
        for w in plan.warnings:
            log.warning(w)
        for x in plan.excluded:
            log.debug("excluded", fields={"resource": x["name"], "uuid": x["uuid"], "reason": x["reason"]})
        log.info("resources discovered", fields={
            "total": plan.total, "targets": len(plan.targets), "excluded": len(plan.excluded)})

        items: list[Item] = []
        waits = []
        for res in plan.targets:
            try:
                new = self._process(res, dry_run, log)
            except Exception as e:  # one broken resource must not stop the run
                log.exception("unexpected error", fields={"resource": res.name, "uuid": res.uuid})
                new = [Item(res, deploy_status="error", change="failed", message=str(e))]
            items += new
            waits += [i for i in new if i.deployment_uuid and i.deploy_status == "queued"]

        if waits and self.cfg.wait_for_deployments and not dry_run:
            log.info("waiting for deployments", fields={"count": len(waits), "timeout_s": self.cfg.deploy_timeout})
            with ThreadPoolExecutor(max_workers=min(8, len(waits))) as pool:
                list(pool.map(lambda i: self._wait(i, log), waits))

        for i in items:
            self._finalize(i, dry_run)
            self._record(run_id, i, log)

        report = self._report(run_id, trigger, dry_run, started, items, plan.excluded, warnings=plan.warnings)
        c = report["counts"]
        status = "success" if not c["failed"] else ("failed" if c["failed"] == len(items) else "partial")
        self.history.finish_run(run_id, status, c)
        log.info("run finished", fields={"status": status, **{k: v for k, v in c.items() if v},
                                         "duration_s": report["duration_s"]})
        notify.dispatch(self.cfg, report, log)
        pruned = self.history.prune(self.cfg.history_retention_days)
        if pruned:
            log.debug("history pruned", fields={"runs": pruned})
        return report

    # --- per resource ----------------------------------------------------------------------------

    def _process(self, res: Resource, dry_run: bool, log) -> list[Item]:
        if res.category == "application":
            return [self._application(res, dry_run, log)]
        if res.category == "service":
            return self._service(res, dry_run, log)
        return [self._database(res, dry_run, log)]

    def _application(self, res: Resource, dry_run: bool, log) -> Item:
        raw = res.raw
        item = Item(res)
        if raw.get("build_pack") == "dockerimage" and raw.get("docker_registry_image_name"):
            image = f"{raw['docker_registry_image_name']}:{raw.get('docker_registry_image_tag') or 'latest'}"
            self._image_versions(item, image, log)
        elif raw.get("git_repository"):
            item.kind = "git"
            item.subject = f"{raw.get('git_repository')}@{raw.get('git_branch') or '?'}"
            try:
                for d in self.coolify.application_deployments(res.uuid):
                    if d.get("status") == "finished" and not d.get("pull_request_id") and d.get("commit"):
                        item.from_version = d["commit"]
                        item.from_label = (d.get("commit_message") or "").split("\n")[0][:80] or None
                        break
            except CoolifyError as e:
                item.notes.append(f"previous commit unknown: {e}")
        self._deploy(item, dry_run, log, lambda: self.coolify.deploy(res.uuid, self.cfg.force_rebuild))
        return item

    def _service(self, res: Resource, dry_run: bool, log) -> list[Item]:
        if not self.cfg.pull_service_images:
            item = Item(res, message="recreated without pulling images (PULL_SERVICE_IMAGES=false)")
            self._deploy(item, dry_run, log, lambda: self.coolify.deploy(res.uuid, self.cfg.force_rebuild))
            return [item]
        try:
            apps = self.coolify.service_applications(res.uuid)
        except CoolifyError as e:
            if e.response is not None and e.response.status == 404:
                return [self._service_fallback(res, dry_run, log, "this Coolify version has no "
                                               "/services/{uuid}/applications endpoint")]
            return [Item(res, deploy_status="error", change="failed", message=str(e),
                         http_status=e.response.status if e.response else None)]
        if not apps:
            return [self._service_fallback(res, dry_run, log, "no containers listed")]
        items = []
        for app in apps:
            item = Item(res, component=str(app.get("name") or app.get("uuid")))
            if app.get("image"):
                self._image_versions(item, str(app["image"]), log)
            app_uuid = str(app.get("uuid"))
            self._deploy(item, dry_run, log, lambda: self.coolify.start_service_application(
                res.uuid, app_uuid, pull_latest=True, force=self.cfg.force_rebuild))
            items.append(item)
        return items

    def _service_fallback(self, res: Resource, dry_run: bool, log, why: str) -> Item:
        item = Item(res, message=f"recreated without pulling images: {why}")
        log.warning("service redeployed without image pull", fields={"resource": res.name, "reason": why})
        self._deploy(item, dry_run, log, lambda: self.coolify.deploy(res.uuid, self.cfg.force_rebuild))
        return item

    def _database(self, res: Resource, dry_run: bool, log) -> Item:
        item = Item(res)
        if res.raw.get("image"):
            self._image_versions(item, str(res.raw["image"]), log)
        self._deploy(item, dry_run, log, lambda: self.coolify.deploy(res.uuid, self.cfg.force_rebuild))
        return item

    # --- versions --------------------------------------------------------------------------------

    def _image_versions(self, item: Item, image: str, log) -> None:
        item.kind = "image"
        item.subject = image
        prev = self.history.image_state(item.resource.uuid, item.component)
        if prev and prev["image"] == image:
            item.from_version, item.from_label = prev["digest"], prev.get("label")
        elif prev:
            item.notes.append(f"image changed in Coolify from {prev['image']}")
            item.from_version, item.from_label = prev["digest"], prev.get("label")
        if not self.cfg.resolve_image_versions:
            return
        ref = parse_image(image)
        if not ref:
            item.notes.append(f"image reference {image!r} cannot be resolved")
            return
        resolved = self.registry.digest(ref)
        if not resolved.digest:
            item.notes.append(f"registry lookup failed: {resolved.error}")
            log.debug("registry lookup failed", fields={"image": image, "error": resolved.error})
            return
        item.to_version = resolved.digest
        cached = self.history.digest_label(resolved.digest)
        if cached is None:
            try:
                version, revision = self.registry.version_info(ref, resolved.digest)
            except Exception as e:  # labels are best effort
                version = revision = None
                log.debug("image version unavailable", fields={"image": image, "error": str(e)})
            self.history.set_digest_label(resolved.digest, version, revision)
            item.to_label = version
        else:
            item.to_label = cached.get("version")

    # --- deploy / wait ---------------------------------------------------------------------------

    def _deploy(self, item: Item, dry_run: bool, log, call) -> None:
        fields = {"resource": item.label, "uuid": item.resource.uuid, "location": item.resource.location}
        if dry_run:
            item.deploy_status = "dry-run"
            log.info("would redeploy", fields=fields | ({"path": item.path} if item.kind != "none" else {}))
            return
        r = call()
        item.http_status = r.status
        data = r.json({}) if r.body else {}
        if not r.ok:
            item.deploy_status = "error"
            item.message = (data.get("message") if isinstance(data, dict) else None) or r.text()
            log.error("deploy request failed", fields=fields | {"http": r.status or "000", "response": item.message})
            return
        message = ""
        if isinstance(data, dict) and isinstance(data.get("deployments"), list) and data["deployments"]:
            first = data["deployments"][0]
            message = first.get("message") or ""
            item.deployment_uuid = first.get("deployment_uuid")
        elif isinstance(data, dict):
            message = data.get("message") or ""
        item.message = message or item.message
        if item.resource.category == "application" and not item.deployment_uuid:
            # e.g. "Deployment already queued for this commit." or an authorization refusal
            item.deploy_status = "not-queued"
            log.warning("deployment not queued", fields=fields | {"response": item.message})
            return
        item.deploy_status = "queued"
        log.info("deploy queued", fields=fields | ({"deployment": item.deployment_uuid} if item.deployment_uuid
                                                   else {}) | {"response": item.message})

    def _wait(self, item: Item, log) -> None:
        deadline = time.time() + self.cfg.deploy_timeout
        while time.time() < deadline:
            try:
                d = self.coolify.deployment(item.deployment_uuid)
            except CoolifyError as e:
                log.debug("deployment poll failed", fields={"deployment": item.deployment_uuid, "error": str(e)})
                d = {}
            status = d.get("status")
            if status in DEPLOYMENT_DONE:
                item.deploy_status = status
                commit = d.get("commit")
                if item.kind == "git" and commit and commit != "HEAD":
                    item.to_version = commit
                    item.to_label = (d.get("commit_message") or "").split("\n")[0][:80] or None
                return
            time.sleep(self.cfg.poll_interval)
        item.deploy_status = "timeout"
        item.notes.append(f"deployment still running after {self.cfg.deploy_timeout}s")

    def _finalize(self, item: Item, dry_run: bool) -> None:
        if item.deploy_status in ("error", "failed", "cancelled-by-user") or item.change == "failed":
            item.change = "failed"
            return
        if item.deploy_status == "not-queued":
            item.change = "skipped"
            return
        if item.kind == "none" or not item.to_version:
            item.change = "unknown"
        elif not item.from_version:
            item.change = "first-seen"
        elif item.from_version == item.to_version:
            item.change = "unchanged"
        else:
            item.change = "upgraded"
        if (item.kind == "image" and item.to_version and not dry_run
                and item.deploy_status in ("queued", "finished", "timeout")):
            self.history.set_image_state(item.resource.uuid, item.component, item.subject,
                                         item.to_version, item.to_label)

    def _record(self, run_id: int, item: Item, log) -> None:
        r = item.resource
        message = "; ".join(x for x in [item.message, *item.notes] if x)
        self.history.add_change(
            run_id, resource_uuid=r.uuid, resource_name=r.name, resource_type=r.type,
            project=r.project, environment=r.environment, component=item.component, kind=item.kind,
            subject=item.subject, from_version=item.from_version, to_version=item.to_version,
            from_label=item.from_label, to_label=item.to_label, change=item.change,
            deploy_status=item.deploy_status, deployment_uuid=item.deployment_uuid,
            http_status=item.http_status, message=message[:1000])
        fields = {"resource": item.label, "uuid": r.uuid, "location": r.location,
                  "status": item.deploy_status}
        if item.kind != "none":
            fields["path"] = item.path
        if message:
            fields["detail"] = message[:300]
        if item.change == "failed":
            log.error("FAILED", fields=fields)
        elif item.change == "upgraded":
            log.info("UPGRADED", fields=fields)
        elif item.notes and item.change in ("unknown", "skipped"):
            log.warning(item.change.upper(), fields=fields)
        else:
            log.info(item.change.upper(), fields=fields)

    def _report(self, run_id, trigger, dry_run, started, items: list[Item], excluded, warnings=None,
                error=None) -> dict:
        counts = {"targets": len({i.resource.uuid for i in items}), "deployed": 0, "upgraded": 0,
                  "unchanged": 0, "first_seen": 0, "unknown": 0, "skipped": 0, "failed": 0,
                  "excluded": len(excluded)}
        for i in items:
            key = i.change.replace("-", "_")
            counts[key] = counts.get(key, 0) + 1
            if i.change != "failed" and i.deploy_status not in ("not-queued", "dry-run"):
                counts["deployed"] += 1

        def entry(i: Item) -> dict:
            return {"resource": i.label, "uuid": i.resource.uuid, "location": i.resource.location,
                    "kind": i.kind, "subject": i.subject, "from": i.from_version, "to": i.to_version,
                    "from_label": i.from_label, "to_label": i.to_label, "path": i.path,
                    "change": i.change, "status": i.deploy_status,
                    "message": "; ".join(x for x in [i.message, *i.notes] if x)}

        return {
            "run_id": run_id, "trigger": trigger, "dry_run": dry_run,
            "duration_s": round(time.time() - started, 1),
            "counts": counts,
            "upgrades": [entry(i) for i in items if i.change == "upgraded"],
            "failures": [entry(i) for i in items if i.change == "failed"],
            "results": [entry(i) for i in items],
            "excluded": excluded,
            "warnings": warnings or [],
            **({"error": error} if error else {}),
        }

"""Coolify API client (endpoints checked against the Coolify v4.7.6 OpenAPI spec and source)."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote, urlencode

from . import http

DEPLOYMENT_DONE = ("finished", "failed", "cancelled-by-user")


class CoolifyError(Exception):
    def __init__(self, message: str, response: http.Response | None = None):
        super().__init__(message)
        self.response = response


@dataclass
class Resource:
    uuid: str
    name: str
    type: str  # application, service, standalone-<engine>
    status: str = ""
    environment_id: int | None = None
    project: str | None = None
    project_uuid: str | None = None
    environment: str | None = None
    environment_uuid: str | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def category(self) -> str:
        if self.type == "application":
            return "application"
        if self.type == "service":
            return "service"
        return "database"

    @property
    def location(self) -> str:
        if self.project or self.environment:
            return f"{self.project or '?'}/{self.environment or '?'}"
        return "?"

    def to_dict(self) -> dict:
        return {"uuid": self.uuid, "name": self.name, "type": self.type, "status": self.status,
                "project": self.project, "environment": self.environment}


class Coolify:
    def __init__(self, base_url: str, token: str, timeout: float = 60):
        self.base = base_url.rstrip("/") + "/api/v1"
        self.headers = {"Authorization": f"Bearer {token}"}
        self.timeout = timeout

    def _call(self, method: str, path: str, *, params: dict | None = None, body=None) -> http.Response:
        url = self.base + path
        if params:
            url += "?" + urlencode(params)
        return http.request(method, url, headers=self.headers, json_body=body, timeout=self.timeout)

    def _get_json(self, path: str, params: dict | None = None):
        r = self._call("GET", path, params=params)
        if not r.ok:
            raise CoolifyError(f"GET {path} failed (HTTP {r.status or '000'}): {r.text()}", r)
        data = r.json()
        if data is None:
            raise CoolifyError(f"GET {path} returned non-JSON: {r.text()}", r)
        return data

    # --- read -----------------------------------------------------------------------------

    def version(self) -> http.Response:
        return self._call("GET", "/version")

    def resources(self) -> list[Resource]:
        out = []
        for item in self._get_json("/resources"):
            if not isinstance(item, dict) or not item.get("uuid"):
                continue
            out.append(Resource(
                uuid=str(item["uuid"]), name=str(item.get("name") or item["uuid"]),
                type=str(item.get("type") or "unknown"), status=str(item.get("status") or ""),
                environment_id=item.get("environment_id"), raw=item))
        return out

    def environment_map(self) -> dict[int, dict]:
        """environment id -> {project, project_uuid, environment, environment_uuid}."""
        envs: dict[int, dict] = {}
        for p in self._get_json("/projects"):
            puuid = p.get("uuid")
            if not puuid:
                continue
            for e in self._get_json(f"/projects/{quote(puuid)}/environments"):
                if e.get("id") is None:
                    continue
                envs[int(e["id"])] = {"project": p.get("name"), "project_uuid": puuid,
                                      "environment": e.get("name"), "environment_uuid": e.get("uuid")}
        return envs

    def application(self, uuid: str) -> dict:
        return self._get_json(f"/applications/{quote(uuid)}")

    def application_deployments(self, uuid: str, take: int = 20) -> list[dict]:
        data = self._get_json(f"/deployments/applications/{quote(uuid)}", {"skip": 0, "take": take})
        if isinstance(data, dict):  # Coolify returns {"count": n, "deployments": [...]}
            data = data.get("deployments") or []
        return [d for d in data if isinstance(d, dict)]

    def deployment(self, deployment_uuid: str) -> dict:
        return self._get_json(f"/deployments/{quote(deployment_uuid)}")

    def service_applications(self, uuid: str) -> list[dict]:
        data = self._get_json(f"/services/{quote(uuid)}/applications")
        return [d for d in data if isinstance(d, dict)]

    def database(self, uuid: str) -> dict:
        return self._get_json(f"/databases/{quote(uuid)}")

    # --- actions --------------------------------------------------------------------------

    def deploy(self, uuid: str, force: bool) -> http.Response:
        """POST /deploy. For a service this recreates the containers but does not pull images."""
        return self._call("POST", "/deploy", body={"uuid": uuid, "force": force})

    def start_service_application(self, service_uuid: str, app_uuid: str, pull_latest: bool,
                                  force: bool) -> http.Response:
        """docker compose pull (when latest) + up -d --no-deps for one container of a service."""
        return self._call(
            "POST", f"/services/{quote(service_uuid)}/applications/{quote(app_uuid)}/start",
            params={"latest": str(pull_latest).lower(), "force": str(force).lower()})

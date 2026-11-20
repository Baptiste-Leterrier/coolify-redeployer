"""In-process fake Coolify API and fake registry used by the tests."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = "1|test-token-abcdef"


class FakeCoolify:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.projects = [{"id": 1, "uuid": "p1", "name": "Shop"}, {"id": 2, "uuid": "p2", "name": "Internal"}]
        self.envs = {"p1": [{"id": 10, "uuid": "e10", "name": "production"},
                            {"id": 11, "uuid": "e11", "name": "staging"}],
                     "p2": [{"id": 20, "uuid": "e20", "name": "production"}]}
        self.resources = [
            {"uuid": "app1", "name": "shop-api", "type": "application", "environment_id": 10,
             "build_pack": "nixpacks", "git_repository": "acme/shop-api", "git_branch": "main"},
            {"uuid": "app2", "name": "shop-front", "type": "application", "environment_id": 11,
             "build_pack": "dockerimage", "docker_registry_image_name": "ghcr.io/acme/front",
             "docker_registry_image_tag": "latest"},
            {"uuid": "svc1", "name": "uptime", "type": "service", "environment_id": 20},
            {"uuid": "db1", "name": "pg", "type": "standalone-postgresql", "environment_id": 10,
             "image": "postgres:16"},
            {"uuid": "self1", "name": "redeployer", "type": "application", "environment_id": 20,
             "build_pack": "dockercompose", "git_repository": "me/coolify-redeployer", "git_branch": "main"},
        ]
        self.service_apps = {"svc1": [{"uuid": "sa1", "name": "uptime-kuma", "image": "louislam/uptime-kuma:1"}]}
        self.app_deployments = {"app1": [{"status": "finished", "commit": "a" * 40,
                                          "commit_message": "old commit", "pull_request_id": 0}]}
        self.deployments: dict[str, dict] = {}
        self.deploy_status = "finished"
        self.next_commit = "b" * 40
        self.fail_deploy: set[str] = set()
        self._n = 0

    def handle(self, method: str, path: str, query: dict, body: dict | None):
        self.calls.append((method, path + ("?" + "&".join(f"{k}={v[0]}" for k, v in sorted(query.items()))
                                           if query else "")))
        p = path.removeprefix("/api/v1")
        if p == "/version":
            return 200, "4.7.6"
        if p == "/resources":
            return 200, self.resources
        if p == "/projects":
            return 200, self.projects
        if p.startswith("/projects/") and p.endswith("/environments"):
            return 200, self.envs.get(p.split("/")[2], [])
        if p.startswith("/deployments/applications/"):
            uuid = p.split("/")[3]
            return 200, {"count": 1, "deployments": self.app_deployments.get(uuid, [])}
        if p.startswith("/deployments/"):
            d = self.deployments.get(p.split("/")[2])
            return (200, d) if d else (404, {"message": "Deployment not found."})
        if p.startswith("/services/") and p.endswith("/applications"):
            return 200, self.service_apps.get(p.split("/")[2], [])
        if p.startswith("/services/") and p.endswith("/start"):
            return 200, {"message": "Service application deploy request queued."}
        if p == "/deploy" and method == "POST":
            uuid = body["uuid"]
            if uuid in self.fail_deploy:
                return 403, {"message": "Missing required permissions: deploy"}
            res = next(r for r in self.resources if r["uuid"] == uuid)
            if res["type"] == "application":
                self._n += 1
                duuid = f"dep{self._n}"
                self.deployments[duuid] = {"deployment_uuid": duuid, "status": self.deploy_status,
                                           "commit": self.next_commit, "commit_message": "new commit"}
                return 200, {"deployments": [{"message": f"Application {res['name']} deployment queued.",
                                              "resource_uuid": uuid, "deployment_uuid": duuid}]}
            return 200, {"deployments": [{"message": f"{res['name']} started.", "resource_uuid": uuid,
                                          "deployment_uuid": None}]}
        return 404, {"message": "Not found."}


class FakeRegistry:
    """Stands in for redeployer.registry.Registry."""

    def __init__(self):
        self.digests = {"ghcr.io/acme/front:latest": "sha256:" + "1" * 64,
                        "louislam/uptime-kuma:1": "sha256:" + "2" * 64,
                        "postgres:16": "sha256:" + "3" * 64}
        self.labels_by_digest = {"sha256:" + "1" * 64: {"org.opencontainers.image.version": "1.0.0"}}
        self.label_calls = 0

    def digest(self, ref):
        from redeployer.registry import Resolved
        d = self.digests.get(ref.original)
        return Resolved(d) if d else Resolved(None, "not found")

    def version_info(self, ref, digest):
        self.label_calls += 1
        labels = self.labels_by_digest.get(digest, {})
        return labels.get("org.opencontainers.image.version"), labels.get("org.opencontainers.image.revision")


def serve_fake(fake: FakeCoolify):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _do(self):
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                return self._out(401, {"message": "Unauthenticated."})
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            status, payload = fake.handle(self.command, u.path, parse_qs(u.query), body)
            self._out(status, payload)

        def _out(self, status, payload):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _do

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"

import base64
import io
import json
import logging
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from redeployer import config, logs
from redeployer.coolify import Coolify
from redeployer.engine import Engine
from redeployer.history import History
from redeployer.server import Health, Scheduler, make_handler

from .fakes import TOKEN, FakeCoolify, FakeRegistry, serve_fake

TRIGGER = "t" * 32


class Sink:
    """Captures ntfy / Novu POSTs."""

    def __init__(self):
        self.posts = []
        sink = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                sink.posts.append((self.path, dict(self.headers), json.loads(self.rfile.read(n))))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeCoolify()
        self.srv, self.url = serve_fake(self.fake)
        self.sink = Sink()
        self.registry = FakeRegistry()
        logs.setup("DEBUG", "text", [TOKEN, TRIGGER])
        logging.getLogger(logs.LOGGER_NAME).handlers[0].setStream(io.StringIO())

    def tearDown(self):
        for srv in (self.srv, self.sink.srv):
            srv.shutdown()
            srv.server_close()
        self.tmp.cleanup()

    def engine(self, **env):
        base = {"TOKEN": TOKEN, "COOLIFY_URL": self.url, "DATA_DIR": self.tmp.name, "POLL_INTERVAL": "2",
                "SELF_UUID": "self1", "TRIGGER_TOKEN": TRIGGER,
                "NTFY_URL": self.sink.url + "/alerts", "NTFY_TOKEN": "tk_secret",
                "NOVU_API_KEY": "novukey", "NOVU_WORKFLOW_ID": "redeploy", "NOVU_SUBSCRIBER_ID": "ops",
                "NOVU_API_URL": self.sink.url}
        base.update(env)
        cfg = config.load(base)
        history = History(cfg.data_dir)
        logs.attach_run_capture(logging.getLogger(logs.LOGGER_NAME), history.add_log)
        return Engine(cfg, history, Coolify(cfg.coolify_url, cfg.token), self.registry)

    def test_full_cycle(self):
        eng = self.engine(EXCLUDE="env:Shop/staging")
        plan = eng.plan()
        self.assertEqual(sorted(r.uuid for r in plan.targets), ["app1", "svc1"])
        reasons = {x["uuid"]: x["reason"] for x in plan.excluded}
        self.assertEqual(reasons, {"app2": "EXCLUDE env:Shop/staging", "db1": "type standalone-postgresql not in "
                                   "RESOURCE_TYPES", "self1": "this redeployer itself"})

        report = eng.run("cli")
        c = report["counts"]
        self.assertEqual((c["upgraded"], c["first_seen"], c["failed"]), (1, 1, 0))
        up = report["upgrades"][0]
        self.assertEqual(up["resource"], "shop-api")
        self.assertIn("aaaaaaa", up["path"])
        self.assertIn("bbbbbbb", up["path"])
        # service container redeployed with the image pull flag, not POST /deploy
        self.assertIn(("POST", "/api/v1/services/svc1/applications/sa1/start?force=false&latest=true"),
                      self.fake.calls)
        self.assertNotIn("svc1", json.dumps([c for c in self.fake.calls if c[1] == "/api/v1/deploy"]))

        # second run: the service image moves to a new digest -> upgraded with versions
        self.fake.app_deployments["app1"] = [{"status": "finished", "commit": "b" * 40, "pull_request_id": 0}]
        new = "sha256:" + "9" * 64
        self.registry.digests["louislam/uptime-kuma:1"] = new
        self.registry.labels_by_digest[new] = {"org.opencontainers.image.version": "1.23.17"}
        report = eng.run("api")
        ups = {u["resource"]: u for u in report["upgrades"]}
        self.assertEqual(set(ups), {"uptime/uptime-kuma"})
        self.assertIn("-> 1.23.17 (sha256:999999999999)", ups["uptime/uptime-kuma"]["path"])
        self.assertEqual(report["counts"]["unchanged"], 1)

        hist = eng.history.changes(change="upgraded")
        self.assertEqual(len(hist), 2)
        self.assertTrue(eng.history.logs(report["run_id"]))

        # alerts: ntfy JSON publish at the server root + Novu trigger
        paths = [p for p, _, _ in self.sink.posts]
        self.assertEqual(paths.count("/"), 2)
        self.assertEqual(paths.count("/v1/events/trigger"), 2)
        ntfy = next(b for p, _, b in self.sink.posts if p == "/")
        self.assertEqual(ntfy["topic"], "alerts")
        novu_hdr = next(h for p, h, _ in self.sink.posts if p == "/v1/events/trigger")
        self.assertEqual(novu_hdr["Authorization"], "ApiKey novukey")

    def test_uuids_allowlist_and_failure(self):
        eng = self.engine(UUIDS="app1 svc1 missing1", EXCLUDE="svc1", NOTIFY_ON="failure")
        self.fake.fail_deploy.add("app1")
        report = eng.run("cli")
        self.assertEqual(report["counts"]["failed"], 1)
        self.assertEqual(report["failures"][0]["message"], "Missing required permissions: deploy")
        self.assertTrue(any("missing1" in w for w in report["warnings"]))
        self.assertEqual(len([p for p in self.sink.posts if p[0] == "/"]), 1)

    def test_dry_run_deploys_nothing(self):
        eng = self.engine()
        report = eng.run("cli", dry_run=True)
        self.assertFalse([c for c in self.fake.calls if c[0] == "POST"])
        self.assertEqual(report["counts"]["deployed"], 0)
        self.assertEqual(self.sink.posts, [])
        self.assertEqual(eng.history.changes(), [])  # dry runs hidden from history by default

    def test_http_api(self):
        eng = self.engine()
        health = Health(eng)
        health.check()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(eng, health, Scheduler(eng)))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        def call(method, path, token=None, body=None, basic=False, accept=None):
            req = urllib.request.Request(base + path, method=method,
                                         data=json.dumps(body).encode() if body is not None else None)
            if token:
                req.add_header("Authorization", "Basic " + base64.b64encode(f"x:{token}".encode()).decode()
                               if basic else f"Bearer {token}")
            if accept:
                req.add_header("Accept", accept)
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode()

        try:
            self.assertEqual(call("GET", "/healthz")[0], 200)
            self.assertEqual(call("POST", "/trigger")[0], 401)
            self.assertEqual(call("POST", "/trigger", token="wrong" * 8)[0], 401)
            self.assertEqual(call("POST", "/trigger", token=TRIGGER, body={"uuids": ["x;rm"]})[0], 400)
            status, out = call("POST", "/trigger", token=TRIGGER, body={"wait": True, "uuids": ["app1"]})
            self.assertEqual(status, 200, out)
            report = json.loads(out)
            self.assertEqual(report["counts"]["targets"], 1)
            status, out = call("POST", "/trigger", token=TRIGGER, body={})
            self.assertEqual(status, 202, out)
            run_id = json.loads(out)["run_id"]
            for _ in range(50):
                run = json.loads(call("GET", f"/runs/{run_id}", token=TRIGGER)[1])
                if run["status"] != "running":
                    break
                threading.Event().wait(0.2)
            self.assertEqual(run["status"], "success")
            status, out = call("GET", "/history", token=TRIGGER, basic=True, accept="text/html")
            self.assertEqual(status, 200)
            self.assertIn("<table>", out)
            self.assertIn("shop-api", out)
            status, out = call("GET", f"/runs/{run_id}/logs?format=text", token=TRIGGER)
            self.assertIn("run finished", out)
            self.assertNotIn(TOKEN, out)
            status, out = call("GET", "/plan", token=TRIGGER)
            self.assertEqual(status, 200)
            self.assertEqual(len(json.loads(out)["targets"]), 3)  # app1, app2, svc1
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()

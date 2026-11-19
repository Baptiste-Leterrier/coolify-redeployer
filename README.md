# coolify-redeployer

A Coolify resource that redeploys your other Coolify resources on a cron schedule (`CRON_SCHEDULE`, default every day at midnight Europe/Paris) or on demand through an HTTP `POST`, using the Coolify API. It keeps a history of every redeploy with the version before and after, and can send alerts to [ntfy](https://ntfy.sh) or [Novu](https://novu.co).

It is a small Python program (standard library only, no third-party package) built from this repository on `python:3.13-alpine`.

## Why ?

The apps on my Coolify instance use images that get updated upstream. Picking up a new image means redeploying the app, and doing that by hand for every app is a chore I will forget.

This service does it for me: on a schedule it asks Coolify to redeploy every resource (or the ones I pick), through Coolify's own API. That gives:

- **Nothing to remember**: the redeploys happen at night without me, and new resources are picked up automatically.
- **Coolify stays in charge**: every redeploy is a normal Coolify deployment. No Docker socket.
- **Easy to change**: what to redeploy, what to skip, the schedule and the alerts are environment variables.
- **Visible**: one log line per resource in the Runtime Logs, a history of what changed (`1.23.16 -> 1.23.17`, `a1b2c3d -> e4f5a6b`), and an alert when something was upgraded or failed.

### Why not Watchtower?

[Watchtower](https://github.com/containrrr/watchtower) also updates containers when their image changes, but:

- **The original project is archived.** `containrrr/watchtower` was archived in December 2025 (last release v1.7.1): it still runs, but gets no more patches. Community forks exist (e.g. `nickfedor/watchtower`); I have not evaluated them.
- **It works behind Coolify's back.** Watchtower recreates containers directly through the Docker socket. Coolify's own docs say nothing about running it alongside Watchtower, so whether Coolify then shows stale state or reverts the change at its next deployment is untested here. This service goes through Coolify's API, so Coolify stays the source of truth.
- **It needs the Docker socket.** Mounting `/var/run/docker.sock` gives a container full control of the host's Docker. This one only needs a Coolify API token limited to `read` and `deploy`.

The trade-off: Watchtower redeploys only when an image changed, while this service queues a deployment for every selected resource on every run. The history then tells you which ones actually changed.

## Setup

1. In Coolify **Settings** (Advanced), enable **API Access** (it is off by default; if you restrict **Allowed IPs**, include the Docker network the container runs in). Then create an API token (**Keys & Tokens → API tokens**) with the **read** and **deploy** abilities. `deploy` is needed to redeploy; `read` is needed to list resources, follow deployments and for the healthcheck. `write` and `delete` are not needed. The token only sees the resources of its team.
2. In Coolify, create a new resource from this Git repository (public repository, or your fork) with the **Docker Compose** build pack. Coolify builds the image from the `Dockerfile` (`build: .` in `docker-compose.yml`).
3. Under **Advanced**, enable **Connect to predefined network**. Without it the container cannot resolve `coolify:8080` and every call fails with `HTTP 000`.
4. Set the environment variables (see below). Only `TOKEN` is required.
5. Optional, for the manual trigger and the history pages: set `TRIGGER_TOKEN`, then open **Domains** and add a domain for the `redeployer` service on internal port `8080` (e.g. `https://redeploy.example.com:8080`).
6. Deploy. Then check what it will redeploy before the first scheduled run: in the container **Terminal**, run `python -m redeployer plan`.

**Exclude the redeployer itself.** In "all resources" mode it would otherwise redeploy (rebuild and restart) itself during a run. Set `SELF_UUID` to its own resource UUID (from its Coolify URL), or add that UUID to `EXCLUDE`. It logs a warning at startup when neither `SELF_UUID` nor `UUIDS` is set. If Coolify injects `COOLIFY_RESOURCE_UUID` into the container, it is used automatically; Coolify documents that variable for applications, but I have not verified that it is set for Docker Compose resources, so do not rely on it.

### Upgrading from the previous version (inline `alpine` + `crond`)

The previous version was a compose file pasted into Coolify. This one is built from the repository, so create it as described above and delete the old service. `TOKEN`, `UUIDS` and `CRON_SCHEDULE` keep their meaning: with `UUIDS` set, only those resources are redeployed, as before. The token now also needs `read` for discovery (the old healthcheck already required it). `docker exec <container> /tmp/redeploy.sh` becomes `python -m redeployer run`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TOKEN` | required | Coolify API token (`read` + `deploy`). Mark it secret. |
| `COOLIFY_URL` | `http://coolify:8080` | Coolify API base URL, reached over the predefined network. |
| `TZ` | `Europe/Paris` | Timezone of `CRON_SCHEDULE` and of log timestamps. |
| `CRON_SCHEDULE` | `0 0 * * *` | 5-field cron expression, or `@daily`, `@weekly`... `off` disables the schedule (manual runs only). |
| `UUIDS` | empty | Space-separated resource UUIDs. Empty: every resource of the token's team. Set: only these (allowlist). |
| `EXCLUDE` | empty | Resources never to redeploy. See [Selecting resources](#selecting-resources). |
| `RESOURCE_TYPES` | `application service` | Which kinds of resources are redeployed: `application`, `service`, `database`. |
| `SELF_UUID` | empty | This redeployer's own UUID, always excluded. |
| `FORCE_REBUILD` | `false` | Pass `force` (rebuild without cache) to deployments. |
| `PULL_SERVICE_IMAGES` | `true` | Redeploy services container by container with an image pull. See [What a redeploy does](#what-a-redeploy-does). |
| `WAIT_FOR_DEPLOYMENTS` | `true` | Follow application deployments until they finish, to record their final status and commit. |
| `DEPLOY_TIMEOUT` | `1800` | Seconds to wait for application deployments before recording `timeout`. |
| `RESOLVE_IMAGE_VERSIONS` | `true` | Ask the image registry which digest and version each image tag points to. |
| `REGISTRY_CREDENTIALS` | empty | For private images: `host=user:token`, space-separated (e.g. `ghcr.io=me:ghp_xxx docker.io=me:dckr_pat_xxx`). |
| `TRIGGER_TOKEN` | empty | Enables the HTTP API (manual trigger, history). At least 24 characters, e.g. `openssl rand -hex 32`. |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error`. `debug` also lists every excluded resource and HTTP access. |
| `LOG_FORMAT` | `text` | `text` (one readable line per event) or `json` (one JSON object per line, for log shippers). |
| `HISTORY_RETENTION_DAYS` | `180` | Runs older than this are deleted from the history. `0` keeps everything. |
| `NOTIFY_ON` | `upgrade failure` | When to alert: `upgrade` (at least one version changed), `failure`, `always` (every run), `none`. |
| `NTFY_URL` | empty | Full ntfy topic URL, e.g. `https://ntfy.sh/my-redeploys`. Enables ntfy. |
| `NTFY_TOKEN` / `NTFY_USERNAME` + `NTFY_PASSWORD` | empty | ntfy access token, or basic auth, for protected topics. |
| `NTFY_PRIORITY` | auto | 1-5. Auto: 3 for upgrades, 4 when something failed. |
| `NOVU_API_KEY`, `NOVU_WORKFLOW_ID`, `NOVU_SUBSCRIBER_ID` | empty | All three enable Novu. |
| `NOVU_API_URL` | `https://api.novu.co` | Use `https://eu.api.novu.co` for the EU region. |

The container checks every variable at startup and exits with all the errors at once (e.g. `configuration error: TOKEN is required ...; CRON_SCHEDULE must have 5 fields, got 6`). To check a change without restarting: `python -m redeployer check-config` in the Terminal.

### Selecting resources

By default every application and service of the token's team is redeployed, in every project and environment. Narrow it with:

- `RESOURCE_TYPES`: databases are off by default (`database` adds standalone databases, which Coolify restarts).
- `UUIDS`: an allowlist. When set, nothing outside it is redeployed.
- `EXCLUDE`: entries separated by **commas or newlines** (not spaces, so names with spaces work). Patterns are case-insensitive and accept `*`, `?`, `[abc]`.

| Entry | Excludes |
|---|---|
| `abc123def` | the resource with this UUID |
| `name:*-staging` | resources whose name matches |
| `project:Internal` | every resource of a project (name or UUID) |
| `env:preview*` | every resource in a matching environment, in any project |
| `env:Shop/staging` | one environment of one project |
| `type:standalone-*` | resources of a type (`application`, `service`, `standalone-postgresql`...) |

Example: `EXCLUDE=project:Internal, env:*/staging, name:legacy-*, k8s0w0cg4kk8`

If an `EXCLUDE` entry uses `project:` or `env:` and the project list cannot be read, the run stops instead of redeploying resources that should have been excluded.

`python -m redeployer plan` (or `GET /plan`) shows the resources a run would redeploy and, for each excluded one, the reason.

## What a redeploy does

For each selected resource, in order of project, environment and name:

| Resource | Call | Effect |
|---|---|---|
| Application | `POST /api/v1/deploy` `{"uuid", "force"}` | A normal Coolify deployment: pulls the image (Docker Image build pack) or fetches the branch and rebuilds (Git build packs). |
| Service | `POST /api/v1/services/{uuid}/applications/{app}/start?latest=true` per container | `docker compose pull` then `up -d --no-deps` for that container. |
| Database | `POST /api/v1/deploy` | Coolify starts the database. Whether this pulls a newer image is not verified. |

**Why services are not sent to `/deploy`.** In the Coolify source (v4.7.6), `POST /deploy` on a service runs `docker compose up --force-recreate` without pulling, so containers are recreated from the image already on the server and a newer `:latest` is never picked up. The per-container `start?latest=true` endpoint pulls first. It only covers the service's application containers; its database containers (`/services/{uuid}/databases`) are not touched. If this endpoint does not exist on your Coolify version, the service is sent to `/deploy` instead and a warning says the images were not pulled. `PULL_SERVICE_IMAGES=false` forces `/deploy` for services.

Then, with `WAIT_FOR_DEPLOYMENTS=true`, the redeployer polls each application deployment (`GET /api/v1/deployments/{uuid}`) until it is `finished`, `failed` or `cancelled-by-user`. Service and database starts return no deployment ID, so they are recorded as `queued`.

Runs never overlap: a manual trigger during a run gets `409`, and a scheduled run that finds one in progress is skipped with a warning.

## History and versions

Every run records, for each resource (and each container of a service), where it came from and where it went:

| Resource | Version tracked | "From" | "To" |
|---|---|---|---|
| Git application | commit | commit of the last finished deployment | commit of this deployment, once finished |
| Docker Image application, service container, database | image digest + version | digest recorded at the previous run | digest the tag points to in the registry now |

The image version is the `org.opencontainers.image.version` label of the image, or an environment variable named after the image (`NGINX_VERSION` for `nginx`) when the label is missing. Many images set neither, and then only the digest is shown, shortened to 12 characters (`sha256:0f3c9a7e12bd`). The digest is read with a `HEAD` request on the manifest (Docker Hub documents that `HEAD` does not count toward its pull rate limit); the version is read once per new digest and cached. The first run has no "from" for images, so they show as `first-seen`.

Each entry gets a `change`:

| change | Meaning |
|---|---|
| `upgraded` | the version is different from last time |
| `unchanged` | same version: redeployed, nothing new |
| `first-seen` | no previous version to compare with (first run) |
| `unknown` | version not available (private image without credentials, image reference with variables, Dockerfile without Git, service redeployed without pull) |
| `skipped` | Coolify did not queue it (e.g. `Deployment already queued for this commit.`) |
| `failed` | the deploy request or the deployment failed |

The history lives in SQLite on the `redeployer-data` volume (`/data/history.sqlite3`), with the log of each run. Read it from the container **Terminal**:

```
python -m redeployer history                      # newest first
python -m redeployer history --change upgraded
python -m redeployer history --resource shop-api  # name or UUID
python -m redeployer runs
python -m redeployer logs 42                      # full log of run #42
```

```
when              run  resource            project/env          image/repo              from                       to                         change    status
2026-11-20 00:00  #12  shop-api            Shop/production      acme/shop-api@main      fix cart total (a1b2c3d)   add coupons (e4f5a6b)      upgraded  finished
2026-11-20 00:00  #12  uptime/uptime-kuma  Internal/production  louislam/uptime-kuma:1  sha256:70233f4acb51        sha256:9e1b0c33d2aa        upgraded  queued
2026-11-20 00:00  #12  grafana/grafana     Internal/production  grafana/grafana:latest  12.2.1 (sha256:51c0e2b8f1a4) 12.2.1 (sha256:51c0e2b8f1a4) unchanged queued
```

or over HTTP (below): `/history` as JSON, or as a table in a browser.

## HTTP API

Enabled when `TRIGGER_TOKEN` is set. Every endpoint except `/healthz` needs `Authorization: Bearer <TRIGGER_TOKEN>`. A browser can use basic auth instead (any user name, the token as password), which makes `/history` readable as a page. Without `TRIGGER_TOKEN`, only `/healthz` answers. Put it behind HTTPS (the Coolify domain does that).

| Method | Path | |
|---|---|---|
| `POST` | `/trigger` | Start a run. Returns `202 {"run_id": 13, ...}` immediately, or `409` if a run is in progress. |
| `GET` | `/runs`, `/runs/{id}` | Recent runs; one run with its per-resource results. |
| `GET` | `/runs/{id}/logs` | Captured log of a run (`?format=text` for plain text). |
| `GET` | `/history` | Version history. `?resource=<uuid or name>`, `?change=upgraded`, `?limit=N`, `?format=html`. |
| `GET` | `/plan` | What a run would redeploy, and what is excluded and why. |
| `GET` | `/status` | Schedule, next run, Coolify reachability, last runs. |
| `GET` | `/healthz` | No auth. `200` when Coolify answered the last check, `503` otherwise. |

`POST /trigger` takes an optional JSON body:

```
curl -X POST https://redeploy.example.com/trigger \
  -H "Authorization: Bearer $TRIGGER_TOKEN" \
  -d '{"uuids": ["k8s0w0cg4kk8"], "dry_run": false, "wait": false}'
```

- `uuids`: only these resources (exclusions and `RESOURCE_TYPES` still apply). Default: everything selected.
- `dry_run`: resolve versions and report what would happen, deploy nothing. Dry runs are kept out of `/history`.
- `wait`: answer only when the run is over, with the full report (status `200`). Mind your proxy timeout.

This makes it easy to redeploy from CI, a phone shortcut, or another tool without logging into Coolify.

## Alerts

After a run, an alert is sent when `NOTIFY_ON` matches: by default when at least one resource was upgraded or failed. Dry runs never alert. A failed alert is logged as a warning and does not fail the run.

```
Coolify redeploy: 2 upgraded, 1 failed
UPGRADED shop-api (Shop/production) acme/shop-api@main fix cart total (a1b2c3d) -> add coupons (e4f5a6b)
UPGRADED uptime/uptime-kuma (Internal/production) louislam/uptime-kuma:1 sha256:70233f4acb51 -> sha256:9e1b0c33d2aa
FAILED worker (Shop/production): Missing required permissions: deploy
Run #12 (schedule): 14 redeployed, 2 upgraded, 1 failed, 6 excluded, in 184.2s
```

- **ntfy**: published as JSON to the server root of `NTFY_URL` (`POST https://ntfy.sh/` with `"topic": "my-redeploys"`), with the title, the text above, priority and the tags `coolify` and `upgrade`/`failure`. Works with ntfy.sh and self-hosted servers.
- **Novu**: `POST {NOVU_API_URL}/v1/events/trigger` with `Authorization: ApiKey ...`, workflow `NOVU_WORKFLOW_ID` and subscriber `NOVU_SUBSCRIBER_ID`. Create the workflow in Novu first; its steps can use `{{payload.title}}`, `{{payload.text}}`, `{{payload.counts.upgraded}}`, and the `payload.upgrades` / `payload.failures` lists (each item has `resource`, `location`, `path`, `from`, `to`, `from_label`, `to_label`, `message`).

## Logs

Everything goes to stdout, so it appears in the Coolify **Runtime Logs**. Each line has a timestamp in `TZ`, a level, an event, and `key=value` fields, including `run=<id>` for everything that belongs to a run:

```
2026-11-20T00:00:00+01:00 INFO    run started run=12 trigger=schedule dry_run=False
2026-11-20T00:00:01+01:00 INFO    resources discovered run=12 total=21 targets=15 excluded=6
2026-11-20T00:00:01+01:00 INFO    deploy queued run=12 resource=shop-api uuid=k8s0w0cg4kk8 location=Shop/production deployment=ab12cd response="Application shop-api deployment queued."
2026-11-20T00:00:02+01:00 ERROR   deploy request failed run=12 resource=worker uuid=x1y2 location=Shop/production http=403 response="Missing required permissions: deploy"
2026-11-20T00:03:04+01:00 INFO    UPGRADED run=12 resource=shop-api uuid=k8s0w0cg4kk8 location=Shop/production status=finished path="acme/shop-api@main fix cart total (a1b2c3d) -> add coupons (e4f5a6b)"
2026-11-20T00:03:04+01:00 INFO    run finished run=12 status=partial targets=15 deployed=14 upgraded=2 unchanged=11 failed=1 excluded=6 duration_s=184.2
```

- `LOG_FORMAT=json` prints the same events as one JSON object per line (`time`, `level`, `msg`, `run` and the fields) for Loki, Vector, or any log drain.
- `LOG_LEVEL=debug` adds every excluded resource with its reason, registry lookups and HTTP access lines.
- The lines of each run are also stored with the history, so `python -m redeployer logs <run>` or `GET /runs/<run>/logs` still shows them after the Runtime Logs have rotated.
- `TOKEN`, `TRIGGER_TOKEN`, the ntfy/Novu secrets and registry passwords are replaced by `***` if they ever appear in a log line.

A run's final status is `success`, `partial` (some failed) or `failed` (all failed, or discovery failed). A run cut by a container restart is marked `interrupted` at the next start.

## Health

The container checks `GET /api/v1/version` with the token every minute, and the Docker healthcheck reads the result from `/healthz`. Coolify shows the resource as **unhealthy** when `coolify:8080` is unreachable or the token is rejected, which are the cases where the next run would fail. It is also briefly unhealthy while Coolify itself restarts or upgrades. The reason is in the Runtime Logs (`coolify check failed detail=...`) and in `/healthz`.

## Pitfalls

- **`HTTP 000`** means the container cannot reach `coolify:8080` (Setup step 3), or the API did not answer within the timeout. **`HTTP 401`** (`Unauthenticated.`) means the token is wrong or revoked. **`HTTP 403`** means the token lacks an ability (`Missing required permissions: deploy`), API Access is disabled (`API is disabled.`), or the container's IP is not in **Allowed IPs** (`You are not allowed to access the API.`); the logged response tells which. **`HTTP 404`** (`No resources found.`) means the UUID does not exist in the token's team.
- **A `200` is not always a deployment.** Coolify can answer `Deployment already queued for this commit.` without queuing anything; it is recorded as `skipped`.
- **Registry lookups need outbound HTTPS** to the registries of your images (Docker Hub, ghcr.io...). Without it, versions are `unknown` but redeploys still happen. Private images need `REGISTRY_CREDENTIALS`.
- **The version shown is what the registry serves for that tag, for the architecture of the Coolify host running this container.** A resource on a server with another architecture pulls a different manifest; its digest is still compared run to run, but the version label is the one of this host's architecture.
- **`container_name` is ignored by Coolify**, which names containers after the service and the resource UUID. Use the **Terminal** tab rather than `docker exec` with a guessed name.

## Development

```
python -m unittest discover -s tests -t .     # unit tests + end-to-end against a fake Coolify API
docker build -t coolify-redeployer .
```

Coolify endpoints and behaviours were checked against the Coolify v4.7.6 OpenAPI specification and source code.

## License

[MIT](LICENSE)

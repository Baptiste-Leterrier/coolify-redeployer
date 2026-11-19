"""Configuration read from environment variables, validated once at startup."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .cron import CronExpression, CronError
from .selectors import Selector, SelectorError, parse_selectors

UUID_RE = re.compile(r"^[A-Za-z0-9]+$")
VALID_TYPES = ("application", "service", "database")
VALID_NOTIFY_ON = ("upgrade", "failure", "always", "none")


class ConfigError(Exception):
    pass


def _bool(env: dict, name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if raw == "":
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _int(env: dict, name: str, default: int, minimum: int = 0) -> int:
    raw = env.get(name, "").strip()
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _words(raw: str) -> list[str]:
    return [w for w in re.split(r"[\s,]+", raw.strip()) if w]


@dataclass
class NtfyConfig:
    url: str
    token: str = ""
    username: str = ""
    password: str = ""
    priority: int = 0  # 0 = automatic (3 for upgrades, 4 for failures)


@dataclass
class NovuConfig:
    api_key: str
    workflow_id: str
    subscriber_id: str
    api_url: str = "https://api.novu.co"


@dataclass
class Config:
    coolify_url: str
    token: str
    uuids: list[str]
    exclude: list[Selector]
    resource_types: list[str]
    cron: CronExpression | None
    cron_raw: str
    timezone: str
    force_rebuild: bool
    pull_service_images: bool
    wait_for_deployments: bool
    deploy_timeout: int
    poll_interval: int
    resolve_image_versions: bool
    registry_credentials: dict[str, tuple[str, str]]
    self_uuid: str
    data_dir: str
    history_retention_days: int
    trigger_token: str
    http_port: int
    log_level: str
    log_format: str
    notify_on: list[str]
    ntfy: NtfyConfig | None = None
    novu: NovuConfig | None = None
    secrets: list[str] = field(default_factory=list)

    @property
    def api_enabled(self) -> bool:
        return bool(self.trigger_token)


def load(env: dict | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    errors: list[str] = []

    def guard(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ConfigError, CronError, SelectorError) as e:
            errors.append(str(e))
            return None

    token = env.get("TOKEN", "").strip()
    if not token:
        errors.append("TOKEN is required (Coolify API token with the read and deploy abilities)")

    uuids = _words(env.get("UUIDS", ""))
    bad = [u for u in uuids if not UUID_RE.match(u)]
    if bad:
        errors.append(f"UUIDS must contain letters/digits only, invalid: {', '.join(bad)}")

    exclude = guard(parse_selectors, env.get("EXCLUDE", "")) or []

    types = _words(env.get("RESOURCE_TYPES", "application service").lower())
    unknown = [t for t in types if t not in VALID_TYPES]
    if unknown:
        errors.append(f"RESOURCE_TYPES accepts {', '.join(VALID_TYPES)}; unknown: {', '.join(unknown)}")
    if not types:
        errors.append("RESOURCE_TYPES must not be empty")

    cron_raw = env.get("CRON_SCHEDULE", "0 0 * * *").strip()
    cron = None
    if cron_raw.lower() not in ("", "off", "disabled", "none"):
        cron = guard(CronExpression, cron_raw)

    notify_on = _words(env.get("NOTIFY_ON", "upgrade failure").lower())
    unknown = [n for n in notify_on if n not in VALID_NOTIFY_ON]
    if unknown:
        errors.append(f"NOTIFY_ON accepts {', '.join(VALID_NOTIFY_ON)}; unknown: {', '.join(unknown)}")

    creds: dict[str, tuple[str, str]] = {}
    for item in _words(env.get("REGISTRY_CREDENTIALS", "")):
        host, sep, userpass = item.partition("=")
        user, sep2, pw = userpass.partition(":")
        if not (sep and sep2 and host and user and pw):
            errors.append("REGISTRY_CREDENTIALS entries must look like host=user:password")
            continue
        creds[host.lower()] = (user, pw)

    ntfy = None
    if env.get("NTFY_URL", "").strip():
        url = env["NTFY_URL"].strip().rstrip("/")
        if not re.match(r"^https?://[^/]+/[^/]+$", url):
            errors.append("NTFY_URL must be the full topic URL, e.g. https://ntfy.sh/my-topic")
        ntfy = NtfyConfig(
            url=url,
            token=env.get("NTFY_TOKEN", "").strip(),
            username=env.get("NTFY_USERNAME", "").strip(),
            password=env.get("NTFY_PASSWORD", ""),
            priority=guard(_int, env, "NTFY_PRIORITY", 0) or 0,
        )
        if ntfy.priority and not 1 <= ntfy.priority <= 5:
            errors.append("NTFY_PRIORITY must be between 1 and 5")

    novu = None
    novu_keys = ("NOVU_API_KEY", "NOVU_WORKFLOW_ID", "NOVU_SUBSCRIBER_ID")
    present = [k for k in novu_keys if env.get(k, "").strip()]
    if present and len(present) != len(novu_keys):
        missing = [k for k in novu_keys if k not in present]
        errors.append(f"Novu alerts need {', '.join(novu_keys)}; missing: {', '.join(missing)}")
    elif present:
        novu = NovuConfig(
            api_key=env["NOVU_API_KEY"].strip(),
            workflow_id=env["NOVU_WORKFLOW_ID"].strip(),
            subscriber_id=env["NOVU_SUBSCRIBER_ID"].strip(),
            api_url=(env.get("NOVU_API_URL", "").strip() or "https://api.novu.co").rstrip("/"),
        )

    log_level = env.get("LOG_LEVEL", "info").strip().upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        errors.append("LOG_LEVEL must be debug, info, warning or error")
    log_format = env.get("LOG_FORMAT", "text").strip().lower()
    if log_format not in ("text", "json"):
        errors.append("LOG_FORMAT must be text or json")

    trigger_token = env.get("TRIGGER_TOKEN", "").strip()
    if trigger_token and len(trigger_token) < 24:
        errors.append("TRIGGER_TOKEN must be at least 24 characters (e.g. openssl rand -hex 32)")

    cfg = Config(
        coolify_url=(env.get("COOLIFY_URL", "").strip() or "http://coolify:8080").rstrip("/"),
        token=token,
        uuids=uuids,
        exclude=exclude,
        resource_types=types,
        cron=cron,
        cron_raw=cron_raw,
        timezone=env.get("TZ", "").strip() or "UTC",
        force_rebuild=guard(_bool, env, "FORCE_REBUILD", False) or False,
        pull_service_images=bool(guard(_bool, env, "PULL_SERVICE_IMAGES", True)),
        wait_for_deployments=bool(guard(_bool, env, "WAIT_FOR_DEPLOYMENTS", True)),
        deploy_timeout=guard(_int, env, "DEPLOY_TIMEOUT", 1800, 30) or 1800,
        poll_interval=guard(_int, env, "POLL_INTERVAL", 10, 2) or 10,
        resolve_image_versions=bool(guard(_bool, env, "RESOLVE_IMAGE_VERSIONS", True)),
        registry_credentials=creds,
        self_uuid=(env.get("SELF_UUID", "").strip() or env.get("COOLIFY_RESOURCE_UUID", "").strip()),
        data_dir=env.get("DATA_DIR", "").strip() or "/data",
        history_retention_days=guard(_int, env, "HISTORY_RETENTION_DAYS", 180) or 0,
        trigger_token=trigger_token,
        http_port=guard(_int, env, "HTTP_PORT", 8080, 1) or 8080,
        log_level=log_level,
        log_format=log_format,
        notify_on=notify_on,
        ntfy=ntfy,
        novu=novu,
    )
    if errors:
        raise ConfigError("; ".join(errors))
    cfg.secrets = [s for s in (
        cfg.token, cfg.trigger_token,
        cfg.ntfy.token if cfg.ntfy else "", cfg.ntfy.password if cfg.ntfy else "",
        cfg.novu.api_key if cfg.novu else "",
        *(pw for _, pw in cfg.registry_credentials.values()),
    ) if s]
    return cfg

"""Resolve what an image tag points to in its registry, without pulling it.

The digest comes from a HEAD request on the manifest (Docker Hub documents that HEAD
requests do not count toward its pull rate limit). The human-readable version comes from
the `org.opencontainers.image.version` label of the image config, read only the first
time a digest is seen (those GETs count as pulls on Docker Hub).
"""

from __future__ import annotations

import base64
import platform
import re
from dataclasses import dataclass
from urllib.parse import urlencode, urljoin

from . import http

MANIFEST_TYPES = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
DOCKER_HUB = "registry-1.docker.io"


@dataclass(frozen=True)
class ImageRef:
    registry: str
    repository: str
    tag: str | None
    digest: str | None
    original: str

    @property
    def display(self) -> str:
        return self.original


def parse_image(ref: str) -> ImageRef | None:
    """Normalize an image reference the way Docker does. Returns None if it is not resolvable."""
    ref = (ref or "").strip()
    if not ref or "$" in ref or " " in ref:
        return None
    name, digest = ref, None
    if "@" in ref:
        name, _, digest = ref.partition("@")
    tag = None
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name, _, tag = name.rpartition(":")
    first, sep, rest = name.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        registry, repo = first.lower(), rest
    else:
        registry, repo = "docker.io", name
    if registry in ("docker.io", "index.docker.io", "registry-1.docker.io"):
        registry = DOCKER_HUB
        if "/" not in repo:
            repo = "library/" + repo
    if not repo or not re.fullmatch(r"[a-z0-9._/-]+", repo):
        return None
    if not tag and not digest:
        tag = "latest"
    return ImageRef(registry, repo, tag, digest, ref)


def _platform() -> tuple[str, str]:
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)
    return "linux", arch


@dataclass
class Resolved:
    digest: str | None
    error: str = ""


class Registry:
    def __init__(self, credentials: dict[str, tuple[str, str]] | None = None, timeout: float = 20):
        self.credentials = {k.lower(): v for k, v in (credentials or {}).items()}
        self.timeout = timeout
        self._tokens: dict[tuple[str, str], str] = {}

    def _creds(self, registry: str):
        aliases = [registry]
        if registry == DOCKER_HUB:
            aliases += ["docker.io", "index.docker.io"]
        for a in aliases:
            if a in self.credentials:
                return self.credentials[a]
        return None

    def _auth_header(self, ref: ImageRef, challenge: str) -> str | None:
        creds = self._creds(ref.registry)
        scheme, _, params = challenge.partition(" ")
        if scheme.lower() == "basic":
            if not creds:
                return None
            return "Basic " + base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
        if scheme.lower() != "bearer":
            return None
        fields = dict(re.findall(r'(\w+)="([^"]*)"', params))
        realm = fields.get("realm")
        if not realm:
            return None
        key = (ref.registry, ref.repository)
        if key in self._tokens:
            return "Bearer " + self._tokens[key]
        query = {"scope": f"repository:{ref.repository}:pull"}
        if fields.get("service"):
            query["service"] = fields["service"]
        headers = {}
        if creds:
            headers["Authorization"] = "Basic " + base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
        r = http.request("GET", realm + ("&" if "?" in realm else "?") + urlencode(query),
                         headers=headers, timeout=self.timeout)
        data = r.json({}) if r.ok else {}
        token = data.get("token") or data.get("access_token")
        if not token:
            return None
        self._tokens[key] = token
        return "Bearer " + token

    def _get(self, ref: ImageRef, method: str, path: str, accept: str, follow: bool = True) -> http.Response:
        url = f"https://{ref.registry}/v2/{ref.repository}/{path}"
        headers = {"Accept": accept}
        key = (ref.registry, ref.repository)
        if key in self._tokens:
            headers["Authorization"] = "Bearer " + self._tokens[key]
        r = http.request(method, url, headers=headers, timeout=self.timeout, follow_redirects=follow)
        if r.status == 401 and "www-authenticate" in r.headers:
            self._tokens.pop(key, None)
            auth = self._auth_header(ref, r.headers["www-authenticate"])
            if auth:
                headers["Authorization"] = auth
                r = http.request(method, url, headers=headers, timeout=self.timeout, follow_redirects=follow)
        return r

    def digest(self, ref: ImageRef) -> Resolved:
        if ref.digest:
            return Resolved(ref.digest)
        r = self._get(ref, "HEAD", f"manifests/{ref.tag}", MANIFEST_TYPES)
        if r.ok and r.headers.get("docker-content-digest"):
            return Resolved(r.headers["docker-content-digest"])
        if r.ok:  # some registries omit the header on HEAD
            r = self._get(ref, "GET", f"manifests/{ref.tag}", MANIFEST_TYPES)
            if r.ok and r.headers.get("docker-content-digest"):
                return Resolved(r.headers["docker-content-digest"])
        reason = r.error or f"HTTP {r.status}"
        if r.status in (401, 403):
            reason += " (private image? set REGISTRY_CREDENTIALS)"
        return Resolved(None, f"{ref.registry}/{ref.repository}:{ref.tag}: {reason}")

    def version_info(self, ref: ImageRef, digest: str) -> tuple[str | None, str | None]:
        """(version, revision) of an image: the OCI version/revision labels, else an env var named
        after the image (e.g. NGINX_VERSION for nginx). (None, None) when neither exists."""
        cfg = self.config(ref, digest)
        labels = cfg.get("Labels") or {}
        version = labels.get("org.opencontainers.image.version")
        if not version:
            prefix = re.sub(r"[^A-Z0-9]", "_", ref.repository.rsplit("/", 1)[-1].upper()) + "_VERSION="
            version = next((e.split("=", 1)[1] for e in cfg.get("Env") or [] if e.startswith(prefix)), None)
        return version, labels.get("org.opencontainers.image.revision")

    def labels(self, ref: ImageRef, digest: str) -> dict:
        return self.config(ref, digest).get("Labels") or {}

    def config(self, ref: ImageRef, digest: str) -> dict:
        """The `config` section (Labels, Env...) of the image for this platform. {} if unavailable."""
        r = self._get(ref, "GET", f"manifests/{digest}", MANIFEST_TYPES)
        manifest = r.json({}) if r.ok else {}
        if "manifests" in manifest:  # index / manifest list: pick our platform
            os_, arch = _platform()
            chosen = None
            for m in manifest.get("manifests", []):
                p = m.get("platform") or {}
                if p.get("os") == os_ and p.get("architecture") == arch:
                    chosen = m.get("digest")
                    break
            if not chosen:
                return {}
            r = self._get(ref, "GET", f"manifests/{chosen}", MANIFEST_TYPES)
            manifest = r.json({}) if r.ok else {}
        config_digest = (manifest.get("config") or {}).get("digest")
        if not config_digest:
            return {}
        r = self._get(ref, "GET", f"blobs/{config_digest}", "*/*", follow=False)
        if r.status in (301, 302, 303, 307, 308) and r.headers.get("location"):
            # Blob storage (S3, CDN) rejects the registry's Authorization header: follow without it.
            location = urljoin(f"https://{ref.registry}/v2/", r.headers["location"])
            r = http.request("GET", location, headers={"Accept": "*/*"}, timeout=self.timeout)
        cfg = r.json({}) if r.ok else {}
        return cfg.get("config") or {}

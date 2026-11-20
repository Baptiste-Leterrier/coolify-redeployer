"""Tiny JSON-over-HTTP helper on top of urllib (no third-party dependency)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

USER_AGENT = "coolify-redeployer"


@dataclass
class Response:
    status: int  # 0 when no HTTP response was received
    body: bytes
    headers: dict
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self, default=None):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return default

    def text(self, limit: int = 300) -> str:
        return self.body[:limit].decode("utf-8", "replace").strip() or self.error


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_no_redirect_opener = urllib.request.build_opener(_NoRedirect)


def request(method: str, url: str, *, headers: dict | None = None, json_body=None,
            data: bytes | None = None, timeout: float = 30, follow_redirects: bool = True) -> Response:
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    hdrs.update(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        opener = urllib.request.urlopen if follow_redirects else _no_redirect_opener.open
        with opener(req, timeout=timeout) as r:
            return Response(r.status, r.read(), {k.lower(): v for k, v in r.headers.items()})
    except urllib.error.HTTPError as e:
        body = e.read() if e.fp else b""
        return Response(e.code, body, {k.lower(): v for k, v in (e.headers or {}).items()})
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        return Response(0, b"", {}, error=f"{type(e).__name__}: {reason}")

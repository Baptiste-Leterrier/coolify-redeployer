"""EXCLUDE selectors: which discovered resources must never be redeployed.

Entries are separated by commas or newlines (not spaces, so names with spaces work):

    <uuid>                    a resource UUID
    name:<glob>               resource name, e.g. name:*-staging
    project:<glob>            project name or UUID, e.g. project:Internal
    env:<glob>                environment name in any project, e.g. env:preview*
    env:<project>/<env>       environment of one project, e.g. env:Shop/staging
    type:<glob>               resource type: application, service, standalone-postgresql...

Globs use shell wildcards (*, ?, [abc]) and are case-insensitive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatchcase

KINDS = ("uuid", "name", "project", "env", "type")


class SelectorError(ValueError):
    pass


@dataclass(frozen=True)
class Selector:
    kind: str
    pattern: str

    def __str__(self) -> str:
        return self.pattern if self.kind == "uuid" else f"{self.kind}:{self.pattern}"

    def matches(self, res) -> bool:
        p = self.pattern.lower()

        def m(value) -> bool:
            return value is not None and fnmatchcase(str(value).lower(), p)

        if self.kind == "uuid":
            return str(res.uuid).lower() == p
        if self.kind == "name":
            return m(res.name)
        if self.kind == "type":
            return m(res.type)
        if self.kind == "project":
            return m(res.project) or m(res.project_uuid)
        if self.kind == "env":
            if "/" in p:
                proj, _, env = p.partition("/")
                return ((fnmatchcase((res.project or "").lower(), proj)
                         or fnmatchcase((res.project_uuid or "").lower(), proj))
                        and (fnmatchcase((res.environment or "").lower(), env)
                             or fnmatchcase((res.environment_uuid or "").lower(), env)))
            return m(res.environment) or m(res.environment_uuid)
        return False

    @property
    def needs_project_map(self) -> bool:
        return self.kind in ("project", "env")


def parse_selectors(raw: str) -> list[Selector]:
    out: list[Selector] = []
    for item in re.split(r"[,\n]", raw or ""):
        item = item.strip()
        if not item:
            continue
        kind, sep, pattern = item.partition(":")
        if not sep:
            if not re.fullmatch(r"[A-Za-z0-9]+", item):
                raise SelectorError(
                    f"EXCLUDE entry {item!r} is not a UUID; use name:, project:, env: or type: "
                    "(entries are separated by commas, not spaces)")
            out.append(Selector("uuid", item))
            continue
        kind = kind.strip().lower()
        pattern = pattern.strip()
        if kind not in KINDS:
            raise SelectorError(f"EXCLUDE entry {item!r}: unknown prefix {kind!r}, use one of {', '.join(KINDS)}")
        if not pattern:
            raise SelectorError(f"EXCLUDE entry {item!r} has an empty pattern")
        out.append(Selector(kind, pattern))
    return out

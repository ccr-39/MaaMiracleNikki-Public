"""The version in assets/interface.json is the single application version."""
from __future__ import annotations

from functools import total_ordering
import json
import re

from app_paths import RESOURCE_ROOT


@total_ordering
class Version:
    def __init__(self, value):
        match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?", value)
        if not match:
            raise ValueError("版本号必须使用 SemVer，例如 1.2.3")
        major, minor, patch, pre, build = match.groups()
        for part in (pre, build):
            if part is not None and any(not p for p in part.split(".")):
                raise ValueError("版本号包含空标识符")
        self.pre = tuple(pre.split(".")) if pre else ()
        if any(p.isdigit() and len(p) > 1 and p[0] == "0" for p in self.pre):
            raise ValueError("预发布数字标识符不能以零开头")
        self.core = tuple(map(int, (major, minor, patch)))
        self.text = value.removeprefix("v")

    def _key(self):
        return self.core, not self.pre, tuple((0, int(p)) if p.isdigit() else (1, p) for p in self.pre)

    def __eq__(self, other):
        return isinstance(other, Version) and self._key() == other._key()

    def __lt__(self, other):
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() < other._key()

    def __str__(self):
        return self.text


def current_version():
    value = json.loads((RESOURCE_ROOT / "assets/interface.json").read_text(encoding="utf-8"))["version"]
    return str(Version(value))

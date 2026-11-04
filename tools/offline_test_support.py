"""Small helpers for optional, private offline test inputs (never device I/O)."""
from __future__ import annotations

from collections.abc import MutableMapping
import os
from pathlib import Path
from unittest import SkipTest

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_ROOTS = (ROOT / "images/screenshots", ROOT / "images/debug")
REQUESTED_INPUTS = {}


def require_file(path: Path) -> Path:
    path = Path(path)
    private = any(path.resolve().is_relative_to(base) for base in PRIVATE_ROOTS)
    hidden = private and os.environ.get("MMN_TEST_WITHOUT_PRIVATE") == "1"
    entry = REQUESTED_INPUTS.setdefault(str(path.resolve()), {
        "exists": path.is_file(), "disabled": hidden, "tests": set(),
    })
    entry["tests"].add(os.environ.get("PYTEST_CURRENT_TEST", "standalone"))
    if hidden or not path.is_file():
        if not private and not path.resolve().is_relative_to(ROOT / "assets/resource/model/ocr"):
            raise FileNotFoundError(f"Required test input missing: {path}")
        raise SkipTest(
            f"Optional local test input {'disabled for source-only validation' if hidden else 'missing'}: {path}; private screenshots and "
            "downloaded OCR models are intentionally not distributed in Git."
        )
    return path


class LazySamples(MutableMapping):
    """Load only the real evidence a test uses; pure logic needs no captures."""
    def __init__(self, loaders=()):
        self.loaders = dict(loaders)
        self._values = {}

    def __getitem__(self, key):
        if key not in self._values:
            self._values[key] = self.loaders[key]()
        return self._values[key]

    def __setitem__(self, key, value):
        self._values[key] = value
        self.loaders.pop(key, None)

    def __delitem__(self, key):
        self._values.pop(key, None)
        self.loaders.pop(key, None)

    def __iter__(self):
        return iter(dict.fromkeys([*self.loaders, *self._values]))

    def __len__(self):
        return len(set(self.loaders) | set(self._values))

    def copy(self):
        result = LazySamples(self.loaders)
        result._values = dict(self._values)
        return result

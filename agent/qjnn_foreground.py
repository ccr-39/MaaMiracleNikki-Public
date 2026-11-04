from __future__ import annotations

import json
import re
from typing import Callable

from maa.custom_recognition import CustomRecognition

from adb_controller import run_process


_FOREGROUND_PATTERNS = (
    re.compile(
        r"(?:topResumedActivity|mResumedActivity|ResumedActivity)\s*[=:]\s*"
        r".*?\bu\d+\s+([A-Za-z][A-Za-z0-9_.]*)/"
    ),
    re.compile(
        r"(?:mCurrentFocus|mFocusedApp)\s*[=:]\s*"
        r".*?\b([A-Za-z][A-Za-z0-9_.]*)/[A-Za-z0-9_.$]+"
    ),
)


def parse_focused_package(output: str) -> str | None:
    """Extract the resumed/focused package from Android dumpsys output."""
    for pattern in _FOREGROUND_PATTERNS:
        match = pattern.search(output)
        if match:
            return match.group(1)
    return None


def focused_package(
    controller,
    *,
    process_runner: Callable = run_process,
) -> str | None:
    """Query foreground state without re-entering Maa's active controller job queue."""
    try:
        info = controller.info
        if callable(info):
            info = info()
        adb_path = info.get("adb_path", "")
        serial = info.get("adb_serial", "")
        if not isinstance(adb_path, str) or not adb_path:
            return None
        if not isinstance(serial, str) or not serial:
            return None

        base_command = [adb_path, "-s", serial, "shell", "dumpsys"]
        for service_args in (("activity", "activities"), ("window", "windows")):
            result = process_runner(
                [*base_command, *service_args],
                timeout=5,
            )
            if result.returncode != 0:
                continue
            package = parse_focused_package(result.stdout or "")
            if package:
                return package
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return None


def launcher_package(controller, *, process_runner: Callable = run_process) -> str | None:
    """Resolve the device's HOME app without screenshots or page text."""
    try:
        info = controller.info
        info = info() if callable(info) else info
        adb_path, serial = info.get("adb_path"), info.get("adb_serial")
        if not isinstance(adb_path, str) or not adb_path or not isinstance(serial, str) or not serial:
            return None
        result = process_runner(
            [adb_path, "-s", serial, "shell", "cmd", "package", "resolve-activity",
             "--brief", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME"],
            timeout=5,
        )
        if result.returncode == 0:
            matches = re.findall(r"(?m)^([A-Za-z][A-Za-z0-9_.]*)/[A-Za-z0-9_.$]+\s*$", result.stdout or "")
            return matches[0] if len(matches) == 1 and matches[0] != "android" else None
    except (OSError, RuntimeError, TypeError, ValueError):
        pass
    return None


class PackageForegroundRecognition(CustomRecognition):
    """Match only when the configured package is positively foreground."""

    def __init__(self, package_resolver: Callable = focused_package) -> None:
        super().__init__()
        self._package_resolver = package_resolver

    def analyze(self, context, argv: CustomRecognition.AnalyzeArg):
        try:
            param = json.loads(argv.custom_recognition_param or "{}")
            expected_package = param.get("package", "")
        except (TypeError, json.JSONDecodeError):
            expected_package = ""

        if not isinstance(expected_package, str) or not expected_package:
            return CustomRecognition.AnalyzeResult(
                box=None,
                detail={"error": "package 参数为空", "foreground": False},
            )

        actual_package = self._package_resolver(context.tasker.controller)
        matched = actual_package == expected_package
        return CustomRecognition.AnalyzeResult(
            box=[0, 0, 1, 1] if matched else None,
            detail={
                "package": expected_package,
                "focused_package": actual_package,
                "foreground": matched,
            },
        )

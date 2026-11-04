"""Unified offline entry: python -m pytest tools -q -ra

Use --without-private-samples to exercise source-only behavior without moving
files. --sample-report=<temporary JSON path> records requested inputs, their
Git tracking/availability and requesting tests, never image pixels or OCR text.
Install pytest plus the existing agent/gui requirements before running; no
command here installs dependencies, connects a device or runs game tasks.
"""
import json
import os
from pathlib import Path
import subprocess

from offline_test_support import REQUESTED_INPUTS, ROOT


def pytest_addoption(parser):
    parser.addoption("--without-private-samples", action="store_true",
                     help="Simulate a checkout without ignored private captures; no files are moved")
    parser.addoption("--sample-report", metavar="PATH",
                     help="Write the inventory of inputs actually requested by these tests")


def pytest_configure(config):
    config._mmn_previous_sample_mode = os.environ.get("MMN_TEST_WITHOUT_PRIVATE")
    if config.getoption("--without-private-samples"):
        os.environ["MMN_TEST_WITHOUT_PRIVATE"] = "1"


def pytest_unconfigure(config):
    previous = config._mmn_previous_sample_mode
    if previous is None:
        os.environ.pop("MMN_TEST_WITHOUT_PRIVATE", None)
    else:
        os.environ["MMN_TEST_WITHOUT_PRIVATE"] = previous


def pytest_sessionfinish(session, exitstatus):
    target = session.config.getoption("--sample-report")
    if not target:
        return
    tracked = set(subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=ROOT,
    ).decode("utf-8").split("\0"))
    rows = []
    for filename, entry in sorted(REQUESTED_INPUTS.items()):
        path = Path(filename)
        name = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
        rows.append({"path": name, "tracked": name in tracked,
                     **entry, "tests": sorted(entry["tests"])})
    Path(target).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

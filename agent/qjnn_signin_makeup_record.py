"""Diagnostic snapshots for makeup; legacy transactions remain compatible."""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from threading import Lock

from app_paths import DATA_ROOT

MAKEUP_RECORD = DATA_ROOT / "config" / "signin_makeup_once.json"
_ACTIVE_ATTEMPT = Lock()


def signin_day():
    return datetime.now(timezone(timedelta(hours=8))).date()


class MakeupRecord:
    def __init__(self, path=MAKEUP_RECORD, *, today=None):
        self.path = Path(path)
        self.today = today
        self.day = today or signin_day()
        self.value = None

    @contextmanager
    def transaction(self, *, fresh=False):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if fresh:
            # The current calendar owns eligibility. This file only records
            # this attempt; old contents and abandoned locks are not read.
            if not _ACTIVE_ATTEMPT.acquire(blocking=False):
                raise RuntimeError("补签正在执行，请等待本次处理结束")
            try:
                self.value = None
                yield self
            finally:
                _ACTIVE_ATTEMPT.release()
            return
        lock = self.path.with_suffix(self.path.suffix + ".lock")
        # An abandoned lock is deliberately never reclaimed automatically.
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        try:
            if self.path.exists():
                self.value = json.loads(self.path.read_text(encoding="utf-8"))
                self._validate()
            yield self
        finally:
            lock.unlink()

    def _validate(self):
        value = self.value
        if (not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1
                or value.get("state") not in {"pending", "done"}
                or not isinstance(value.get("date"), str)
                or type(value.get("diamonds")) is not int or value["diamonds"] < 0
                or type(value.get("signed_before")) is not int
                or not 0 <= value["signed_before"] <= 30
                or value.get("phase") not in {"entry", "confirm", "verified"}):
            raise ValueError("补签记录损坏，禁止自动补签")
        recorded_day = date.fromisoformat(value["date"])
        if recorded_day > self.day:
            raise ValueError("补签记录日期异常，禁止自动补签")
        if value["state"] == "done" and value["phase"] != "verified":
            raise ValueError("补签完成记录无效")
        if value["state"] == "pending" and value["phase"] == "verified":
            raise ValueError("补签未决记录无效")

    @property
    def blocked(self):
        # Completed local history does not identify the currently logged-in
        # game account. Only an unresolved input is a payment retry fence.
        return self.value is not None and self.value["state"] == "pending"

    @property
    def pending_today(self):
        return self.blocked and self.value["date"] == self.day.isoformat()

    def _save(self):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(self.value, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def arm(self, diamonds, signed_before, phase):
        if self.today is None and signin_day() != self.day:
            raise ValueError("跨日时禁止继续补签")
        if phase == "entry" and self.blocked:
            if self.pending_today:
                raise ValueError("补签已有记录，禁止重试")
            # A settled calendar and the current day's first fee were checked
            # by the caller. Preserve old uncertainty before starting a new day.
            archive = self.path.with_name(self.path.stem + ".pending-" + self.value["date"] + ".json")
            previous = json.dumps(self.value, ensure_ascii=False, indent=2)
            if archive.exists():
                if json.loads(archive.read_text(encoding="utf-8")) != self.value:
                    raise ValueError("旧补签记录备份冲突")
            else:
                with archive.open("x", encoding="utf-8") as stream:
                    stream.write(previous)
                    stream.flush()
                    os.fsync(stream.fileno())
        if phase == "confirm" and (self.value is None or self.value["state"] != "pending"
                                   or self.value["phase"] != "entry"
                                   or self.value["diamonds"] != diamonds
                                   or self.value["signed_before"] != signed_before):
            raise ValueError("补签确认与原操作不一致")
        self.value = dict(version=1, date=self.day.isoformat(), state="pending", phase=phase,
                          diamonds=diamonds, signed_before=signed_before)
        self._validate()
        self._save()  # Must be durable BEFORE submitting a potentially billable click.

    def complete(self):
        if self.today is None and signin_day() != self.day:
            raise ValueError("跨日时保留未决补签，禁止重试")
        if self.value is None or self.value["state"] != "pending":
            raise ValueError("没有可完成的补签事务")
        self.value = dict(self.value, state="done", phase="verified")
        self._save()

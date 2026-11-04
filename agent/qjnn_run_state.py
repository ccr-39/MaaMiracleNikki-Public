"""In-memory records owned by one complete task queue; no account identity or I/O."""
from contextlib import contextmanager
from copy import deepcopy
from threading import RLock

RECORD_NAMES = frozenset({
    "clothes_shop_daily_state.json",
    "dreamweaver_purchase_state.json",
    "mystery_house_daily_state.json",
    "home_daily_state.json",
    "stamina_daily_state.json",
    "competition_daily_state.json",
    "decompose_daily_state.json",
    "friend_mail_state.json",
})


class RunRecord:
    def __init__(self, name):
        if name not in RECORD_NAMES:
            raise ValueError("unknown run record")
        self.name = name
        self._value = None
        self._closed = False
        self._busy = False
        self._lock = RLock()

    def _check_open(self):
        if self._closed:
            raise RuntimeError("task queue has ended")

    def read(self):
        with self._lock:
            self._check_open()
            return deepcopy(self._value)

    def write(self, value):
        with self._lock:
            self._check_open()
            self._value = deepcopy(value)

    @contextmanager
    def transaction(self):
        with self._lock:
            self._check_open()
            if self._busy:
                raise RuntimeError("purchase already in progress in this queue")
            self._busy = True
        try:
            yield self
        finally:
            with self._lock:
                self._busy = False

    def clear(self):
        with self._lock:
            self._value = None
            self._closed = True


class RunState:
    def __init__(self):
        self._records = {}
        self._closed = False
        self._lock = RLock()

    def record(self, name):
        with self._lock:
            if self._closed:
                raise RuntimeError("task queue has ended")
            if name not in self._records:
                self._records[name] = RunRecord(name)
            return self._records[name]

    def clear(self):
        with self._lock:
            for record in self._records.values():
                record.clear()
            self._records.clear()
            self._closed = True


def run_record(state, name):
    # Standalone actions without a queue owner get an invocation-local record.
    return state.record(name) if state is not None else RunRecord(name)

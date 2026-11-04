"""Room progress for one queue, including automatic recovery attempts; no I/O."""
from qjnn_run_state import RunRecord

HOME_PROGRESS_RECORD = "home_daily_state.json"
RANDOM_VISIT_LIMIT = 5


class HomeTaskProgress:
    def __init__(self, record=None):
        self.record = record if record is not None else RunRecord(HOME_PROGRESS_RECORD)
        if self.record.name != HOME_PROGRESS_RECORD:
            raise ValueError("wrong room progress record")

    def _read(self):
        value = self.record.read()
        if value is None:
            return {"successful_visits": 0, "visit_pending": False, "life_bits_reason": None}
        if not isinstance(value, dict):
            raise ValueError("invalid room progress; refusing to reset visit count")
        count = value.get("successful_visits")
        if (type(count) is not int or not 0 <= count <= max(6, RANDOM_VISIT_LIMIT)
                or type(value.get("visit_pending")) is not bool
                or value.get("life_bits_reason") is not None
                and not isinstance(value["life_bits_reason"], str)):
            raise ValueError("invalid room progress; refusing to reset visit count")
        return value

    @property
    def successful_visits(self):
        return self._read()["successful_visits"]

    @property
    def visit_pending(self):
        return self._read()["visit_pending"]

    @property
    def life_bits_reason(self):
        return self._read()["life_bits_reason"]

    def mark_life_bits(self, reason):
        value = self._read()
        value["life_bits_reason"] = reason
        self.record.write(value)

    def begin_visit(self):
        value = self._read()
        if value["visit_pending"] or value["successful_visits"] >= RANDOM_VISIT_LIMIT:
            raise ValueError("visit already pending or visit limit reached")
        # Fence before submitting input: an exception/timeout must never let a
        # recovery attempt replay an uncertain random-visit click.
        value["visit_pending"] = True
        self.record.write(value)

    def confirm_visit(self, *, count_visit=True):
        value = self._read()
        if not value["visit_pending"] or value["successful_visits"] >= RANDOM_VISIT_LIMIT:
            raise ValueError("no pending visit to confirm")
        if count_visit:
            value["successful_visits"] += 1
        value["visit_pending"] = False
        self.record.write(value)

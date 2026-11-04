"""Coin-only checks and queue-local transactions, with an explicit legacy file adapter."""
from contextlib import contextmanager
from datetime import date
import json
import os
from pathlib import Path
import re
import tempfile
from qjnn_run_state import RunRecord


def coin_quote(items, frame):
    """Read the modal total and currency independently, at 900x1600 scale."""
    import cv2
    height, width = frame.shape[:2]
    def crop(roi):
        x, y, w, h = roi
        return frame[round(y * height / 1600):round((y + h) * height / 1600),
                     round(x * width / 900):round((x + w) * width / 900)]
    amounts = []
    for item in items:
        x, y = item.box.center
        x, y = x * 900 / width, y * 1600 / height
        text = re.sub(r"\s+", "", item.text)
        if item.score >= .8 and 415 <= x <= 615 and 950 <= y <= 1020 and re.fullmatch(r"\d{1,6}", text):
            amounts.append(int(text))
    if len(amounts) != 1 or amounts[0] <= 0:
        return None
    icon = crop((364, 958, 50, 50))
    if not icon.size:
        return None
    hsv = cv2.cvtColor(icon, cv2.COLOR_BGR2HSV)
    gold = (hsv[:, :, 0] >= 16) & (hsv[:, :, 0] <= 42) & (hsv[:, :, 1] >= 65) & (hsv[:, :, 2] >= 110)
    pink = (hsv[:, :, 0] >= 140) & (hsv[:, :, 1] >= 65)
    return amounts[0] if float(gold.mean()) >= .35 and float(pink.mean()) <= .10 else None


class PurchaseJournal:
    @classmethod
    def check_resolved_file(cls, path):
        """Read-only queue gate; never acquire, remove or clear a transaction."""
        path = Path(path)
        lock = path.with_suffix(path.suffix + ".lock")
        if lock.exists():
            raise ValueError("学会购买事务仍被锁定，禁止队列继续")
        cls(path)._load()  # Reuse strict schema/date/pending validation.
        if lock.exists():
            raise ValueError("学会购买事务状态已变化，禁止队列继续")

    def __init__(self, path):
        self.path = path if isinstance(path, RunRecord) else Path(path)
        self.spent = 0
        self.pending = None
        self.products = []
        self.day = date.today().isoformat()

    @contextmanager
    def transaction(self):
        """Serialize purchases and retain uncertain results within the queue."""
        if isinstance(self.path, RunRecord):
            with self.path.transaction():
                self._load()
                yield self
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_suffix(self.path.suffix + ".lock")
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        try:
            self._load()
            yield self
        finally:
            lock.unlink()

    def _load(self):
        self.day = date.today().isoformat()
        self.spent, self.pending = 0, None
        self.products = []
        if isinstance(self.path, RunRecord):
            value = self.path.read()
            if value is None:
                return
        else:
            if not self.path.exists():
                return
            value = json.loads(self.path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or type(value.get("spent")) is not int
                or value["spent"] < 0 or not isinstance(value.get("date"), str)
                or not isinstance(value.get("products"), list)
                or any(not isinstance(name, str) or not name for name in value["products"])
                or "pending" not in value):
            raise ValueError("学会购买记录无效，停止购物")
        date.fromisoformat(value["date"])
        if value["pending"] is not None:
            raise ValueError("本次运行的学会购买结果未确认，停止重复购物")
        if value["date"] == self.day:
            self.spent = value["spent"]
            self.products = value["products"]
        elif value["date"] > self.day:
            raise ValueError("学会购买记录日期晚于当前日期，停止购物")

    def _save(self):
        if isinstance(self.path, RunRecord):
            self.path.write({"date": self.day, "spent": self.spent, "pending": self.pending,
                             "products": self.products})
            return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             delete=False, suffix=".tmp") as stream:
                temporary = Path(stream.name)
                json.dump({"date": self.day, "spent": self.spent, "pending": self.pending,
                           "products": self.products}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def arm(self, amount, product, quantity, owned):
        if (self.pending is not None or type(amount) is not int or amount <= 0
                or date.today().isoformat() != self.day):
            raise ValueError("学会购买状态异常，停止购物")
        self.pending = {"coins": amount, "product": product, "quantity": quantity, "owned_before": owned}
        self._save()  # Arm the transaction fence before submitting any purchase.

    def complete(self):
        self.spent += self.pending["coins"]
        self.products.append(self.pending["product"])
        self.pending = None
        self._save()

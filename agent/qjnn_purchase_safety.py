"""Purchase-only fresh-frame checks and bounded, failure-only evidence."""
from collections import deque
from datetime import datetime
import json
from pathlib import Path
import time
from uuid import uuid4


class PurchaseEvidence:
    def __init__(self, session):
        self.session = session
        self.started = time.monotonic()
        self.events = deque(maxlen=96)
        self.frames = deque(maxlen=2)
        self.before_click = None
        self.capture_number = 0

    def __getattr__(self, name):
        value = getattr(self.session, name)
        if name in {"recognize_purchase_quantity", "recognize_purchase_product"}:
            def read(frame):
                result = value(frame)
                self.note(name, capture=self.capture_number, result=result)
                return result
            return read
        return value

    def note(self, kind, **values):
        self.events.append(dict(kind=kind, elapsed=round(time.monotonic() - self.started, 3), **values))

    def capture_frame(self):
        frame = self.session.capture_frame()
        self.capture_number += 1
        self.frames.append((self.capture_number, frame.copy()))
        return frame

    def recognize_text(self, frame):
        items = self.session.recognize_text(frame)
        self.note("ocr", capture=self.capture_number, items=[dict(text=i.text, score=i.score,
                                    box=[i.box.x, i.box.y, i.box.width, i.box.height]) for i in items])
        return items

    def click(self, x, y):
        if self.frames:
            number, frame = self.frames[-1]
            self.before_click = (number, frame.copy())
        self.note("click", capture=self.capture_number, point=[x, y])
        return self.session.click(x, y)

    def save_failure(self, log):
        root = getattr(self.session, "purchase_evidence_root", None)
        if root is None:
            return
        # One read-only observation after failure, never another input/retry.
        # Keep the original evidence even when the device cannot capture it.
        try:
            self.recognize_text(self.capture_frame())
        except Exception as exc:
            self.note("failure_capture_unavailable", error=str(exc))
        try:
            import cv2
            directory = Path(root) / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8])
            directory.mkdir(parents=True, exist_ok=False)
            images = list(self.frames)
            if self.before_click is not None:
                images.insert(0, self.before_click)
            names = []
            for index, (number, frame) in enumerate(images):
                name = f"frame-{index}.png"
                ok, data = cv2.imencode(".png", frame)
                if not ok:
                    raise OSError("could not encode purchase evidence")
                (directory / name).write_bytes(data.tobytes())
                names.append(dict(name=name, capture=number,
                                  role="before_last_click" if index == 0 and self.before_click is not None else "last_observation"))
            (directory / "evidence.json").write_text(json.dumps(
                dict(images=names, events=list(self.events)), ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"[DreamweaverCommission] purchase failure evidence: {directory}")
        except Exception as exc:  # Diagnostics must never replace the business failure.
            log(f"[DreamweaverCommission] could not save purchase evidence: {exc}")


def stable_purchase_click(session, selector, wait_before_click, blocked, *, before_submit=None):
    """Two fresh observations, one input at most; selectors have no side effects.

    A selector returns (already-inset rectangle, semantic identity). Movement,
    price changes or an overlay cancel the action rather than retrying it.
    """
    wait_before_click(session)
    previous = None
    for index in range(2):
        frame = session.capture_frame()
        items = session.recognize_text(frame)
        target = None if blocked(items, frame) else selector(items, frame)
        if target is None:
            session.note("click_blocked", capture=session.capture_number, reason="page, overlay or target unconfirmed")
            return False
        box, identity = target
        if box.width <= 0 or box.height <= 0:
            return False
        session.note("target", capture=session.capture_number, identity=identity,
                     box=[box.x, box.y, box.width, box.height])
        if previous is not None:
            old, old_identity = previous
            # Tight tolerance scales with the safe region, never with screen size.
            tolerance = max(1, min(old.width, old.height, box.width, box.height) * .05)
            if old_identity != identity or any(abs(a - b) > tolerance for a, b in zip(
                    (old.x, old.y, old.width, old.height), (box.x, box.y, box.width, box.height))):
                session.note("click_blocked", reason="target or semantic identity changed")
                return False
            x, y = box.center
            if not (old.x <= x < old.x + old.width and old.y <= y < old.y + old.height):
                return False
        previous = target
        if index == 0:
            session.sleep(.5)
    if before_submit is not None:
        before_submit(identity)
    return session.click(*box.center) is not False

from __future__ import annotations

import re
from pathlib import Path


class DiagnosticFrameStore:
    """Generic, business-agnostic diagnostic PNG storage."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = Path(output_dir)

    def capture(self, state: str, frame) -> bool:
        try:
            import cv2

            if frame is None or getattr(frame, "size", 0) == 0:
                return False
            self.output_dir.mkdir(parents=True, exist_ok=True)
            safe_state = re.sub(r"[^A-Za-z0-9_-]", "_", state)
            destination = next(
                (
                    self.output_dir / f"{safe_state}_{index:03d}.png"
                    for index in range(1, 1000)
                    if not (self.output_dir / f"{safe_state}_{index:03d}.png").exists()
                ),
                None,
            )
            if destination is None:
                return False
            encoded, data = cv2.imencode(".png", frame)
            if not encoded:
                return False
            data.tofile(str(destination))
            print(f"[DiagnosticFrame] saved: {destination}")
            return True
        except Exception as exc:
            print(f"[DiagnosticFrame] capture failed: {exc}")
            return False

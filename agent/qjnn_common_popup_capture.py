from __future__ import annotations

from app_paths import DATA_ROOT

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMMON_POPUP_CAPTURE_DIR = DATA_ROOT / "images" / "screenshots" / "common_popup"
COMMON_POPUP_CAPTURE_TARGETS = {
    "QJNN_CommonSkillUnlockPopup": "common_skill_unlock_popup",
    "QJNN_CommonSkillUnlockPopupClickMap": "common_skill_unlock_popup_click_map",
    "QJNN_CommonSkillUnlockPopupDismissFailure": "common_skill_unlock_popup_dismiss_failure",
    "QJNN_CommonAchievementPopup": "common_achievement_popup",
    "QJNN_CommonAchievementPopupClickMap": "common_achievement_popup_click_map",
    "QJNN_CommonAchievementPopupDismissFailure": "common_achievement_popup_dismiss_failure",
    "QJNN_CommonPopupUnknown": "common_popup_unknown",
}
COMMON_POPUP_REPEATABLE_CAPTURE_STATES = frozenset(
    {"QJNN_CommonAchievementPopupClickMap", "QJNN_CommonSkillUnlockPopupClickMap"}
)


class CommonPopupCaptureManager:
    def __init__(self, output_dir: Path = COMMON_POPUP_CAPTURE_DIR) -> None:
        self.output_dir = Path(output_dir)
        self._attempted: set[str] = set()

    def _next_path(self, stem: str) -> Path:
        for index in range(1, 1000):
            candidate = self.output_dir / f"{stem}_real_{index:03d}.png"
            if not candidate.exists():
                return candidate
        raise OSError(f"common popup capture indices exhausted: {stem}")

    def capture_once(self, state: str, frame) -> bool:
        stem = COMMON_POPUP_CAPTURE_TARGETS.get(state)
        if stem is None:
            raise ValueError(f"unregistered common popup capture state: {state}")
        repeatable = state in COMMON_POPUP_REPEATABLE_CAPTURE_STATES
        if not repeatable and state in self._attempted:
            return False
        if not repeatable:
            self._attempted.add(state)
        print(f"[CommonPopupCapture] {state}")
        try:
            import cv2

            if frame is None or getattr(frame, "size", 0) == 0:
                raise ValueError("empty frame")
            self.output_dir.mkdir(parents=True, exist_ok=True)
            destination = self._next_path(stem)
            encoded, data = cv2.imencode(".png", frame)
            if not encoded:
                raise OSError("PNG encode failed")
            data.tofile(str(destination))
            print("[CommonPopupCapture] saved:")
            print(destination.resolve())
            return True
        except Exception as exc:
            print(f"[CommonPopupCapture] ERROR: {state}: {exc}")
            return False

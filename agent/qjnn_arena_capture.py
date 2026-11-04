from __future__ import annotations

from app_paths import DATA_ROOT

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARENA_CAPTURE_DIR = DATA_ROOT / "images" / "screenshots" / "arena"

ARENA_CAPTURE_TARGETS = {
    "QJNN_ArenaHomeActive": "arena_home_active",
    "QJNN_ArenaHomeDone": "arena_home_done",
    "QJNN_ArenaBattleQuickFinish": "arena_battle_quick_finish",
    "QJNN_ArenaRewardPopup": "arena_reward_popup",
    "QJNN_ArenaRewardFailure": "arena_reward_failure",
    "QJNN_ArenaBackHome": "arena_back_home",
    "QJNN_ArenaHomeWaitFailure": "arena_home_wait_failure",
    "QJNN_ArenaLoadingTimeout": "arena_loading_timeout",
    "QJNN_ArenaUnknownStart": "arena_unknown_start",
    "QJNN_ArenaExitBackFailure": "arena_exit_back_failure",
}


class ArenaCaptureManager:
    """Save each confirmed arena state once using the active Maa frame."""

    def __init__(self, output_dir: Path = ARENA_CAPTURE_DIR) -> None:
        self.output_dir = Path(output_dir)
        self._attempted: set[str] = set()

    def _next_path(self, stem: str) -> Path:
        for index in range(1, 1000):
            candidate = self.output_dir / f"{stem}_real_{index:03d}.png"
            if not candidate.exists():
                return candidate
        raise OSError(f"竞技场截图编号已用尽：{stem}")

    def capture_once(self, state: str, frame) -> bool:
        stem = ARENA_CAPTURE_TARGETS.get(state)
        if stem is None:
            raise ValueError(f"未登记的竞技场截图状态：{state}")
        if state in self._attempted:
            return False
        self._attempted.add(state)

        print(f"[ArenaCapture] {state}")
        try:
            import cv2

            if frame is None or getattr(frame, "size", 0) == 0:
                raise ValueError("截图为空")
            self.output_dir.mkdir(parents=True, exist_ok=True)
            destination = self._next_path(stem)
            encoded, data = cv2.imencode(".png", frame)
            if not encoded:
                raise OSError("PNG 编码失败")
            data.tofile(str(destination))
            print("[ArenaCapture] saved:")
            print(destination.resolve())
            return True
        except Exception as exc:
            print(f"[ArenaCapture] ERROR: {state}: {exc}")
            return False

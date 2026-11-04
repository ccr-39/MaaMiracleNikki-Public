from __future__ import annotations

from app_paths import DATA_ROOT

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPETITION_CAPTURE_DIR = DATA_ROOT / "images" / "screenshots" / "competition"

COMPETITION_CAPTURE_TARGETS = {
    "QJNN_CompetitionHome": "competition_home",
    "QJNN_CompetitionJudgeActive": "competition_judge_active",
    "QJNN_CompetitionJudgeDone": "competition_judge_done",
    "QJNN_CompetitionBackHome": "competition_back_home",
    "QJNN_CompetitionHomeWaitFailure": "competition_home_wait_failure",
    "QJNN_CompetitionJudgeWaitFailure": "competition_judge_wait_failure",
    "QJNN_CompetitionNextPairWaitFailure": "competition_next_pair_wait_failure",
    "QJNN_CompetitionJudgeBackFailure": "competition_judge_back_failure",
    "QJNN_CompetitionHomeBackFailure": "competition_home_back_failure",
    "QJNN_CompetitionUnknownStart": "competition_unknown_start",
}


class CompetitionCaptureManager:
    """Save each confirmed Competition state once from the active Maa frame."""

    def __init__(self, output_dir: Path = COMPETITION_CAPTURE_DIR) -> None:
        self.output_dir = Path(output_dir)
        self._attempted: set[str] = set()

    def _next_path(self, stem: str) -> Path:
        for index in range(1, 1000):
            candidate = self.output_dir / f"{stem}_real_{index:03d}.png"
            if not candidate.exists():
                return candidate
        raise OSError(f"搭配评选赛截图编号已用尽：{stem}")

    def capture_once(self, state: str, frame) -> bool:
        stem = COMPETITION_CAPTURE_TARGETS.get(state)
        if stem is None:
            raise ValueError(f"未登记的搭配评选赛截图状态：{state}")
        if state in self._attempted:
            return False
        self._attempted.add(state)

        print(f"[CompetitionCapture] {state}")
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
            print("[CompetitionCapture] saved:")
            print(destination.resolve())
            return True
        except Exception as exc:
            print(f"[CompetitionCapture] ERROR: {state}: {exc}")
            return False

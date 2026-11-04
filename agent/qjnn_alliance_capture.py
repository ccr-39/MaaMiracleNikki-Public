from __future__ import annotations

from app_paths import DATA_ROOT

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALLIANCE_CAPTURE_DIR = DATA_ROOT / "images" / "screenshots" / "alliance"

ALLIANCE_CAPTURE_TARGETS = {
    "QJNN_AllianceCommissionMapActive": "alliance_commission_map_active",
    "QJNN_AllianceCommissionDetailActive": "alliance_commission_detail_active",
    "QJNN_AllianceCommissionSettlement": "alliance_commission_settlement",
    "QJNN_AllianceCommissionPostSettlementPopup": (
        "alliance_commission_post_settlement_popup"
    ),
    "QJNN_AllianceLibraryFree": "alliance_library_free",
    "QJNN_AllianceLibraryReward": "alliance_library_reward",
    "QJNN_AllianceDailyUnknownStart": "alliance_daily_unknown_start",
}


class AllianceCaptureManager:
    """Save each explicitly selected alliance state once without device access."""

    def __init__(self, output_dir: Path = ALLIANCE_CAPTURE_DIR) -> None:
        self.output_dir = Path(output_dir)
        self._attempted: set[str] = set()

    def _next_path(self, stem: str) -> Path:
        for index in range(1, 1000):
            candidate = self.output_dir / f"{stem}_real_{index:03d}.png"
            if not candidate.exists():
                return candidate
        raise OSError(f"联盟截图编号已用尽：{stem}")

    def capture_once(self, state: str, frame) -> bool:
        stem = ALLIANCE_CAPTURE_TARGETS.get(state)
        if stem is None:
            raise ValueError(f"未登记的联盟截图状态：{state}")
        if state in self._attempted:
            return False
        self._attempted.add(state)

        print(f"[AllianceCapture] {state}")
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
            print("[AllianceCapture] saved:")
            print(destination.resolve())
            return True
        except Exception as exc:
            print(f"[AllianceCapture] ERROR: {state}: {exc}")
            return False

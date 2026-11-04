from __future__ import annotations

from app_paths import DATA_ROOT

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from qjnn_diagnostic import DiagnosticFrameStore
from qjnn_home import detect_home_page
from qjnn_home_life_bits import (
    HomeLifeBitsResult,
    _make_runtime_session,
    run_home_life_bits,
)
from qjnn_home_navigation import navigate_home_page
from qjnn_home_random_visit import (
    RandomVisitResult,
    RandomVisitState,
    detect_random_visit_state,
    run_home_random_visit,
)
from qjnn_home_room_entry import exit_home_room
from qjnn_home_progress import HomeTaskProgress

try:
    from maa.custom_action import CustomAction
except ImportError:  # MaaFramework is optional for offline tests.
    CustomAction = object


PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOME_DAILY_DIAGNOSTIC_DIR = (
    DATA_ROOT / "images" / "debug" / "pipeline" / "home_daily"
)


@dataclass(frozen=True)
class HomeDailyResult:
    success: bool
    reason: str
    life_bits_reason: str = "not_started"
    random_visit_reason: str = "not_started"


def run_home_daily(
    session,
    *,
    capture: Callable[[str, object], bool] | None = None,
    log: Callable[[str], None] = print,
    page_detector: Callable[[object], object] = detect_home_page,
    life_bits_runner: Callable[..., HomeLifeBitsResult] = run_home_life_bits,
    random_visit_runner: Callable[..., RandomVisitResult] = run_home_random_visit,
    navigator: Callable[..., object] = navigate_home_page,
    random_visit_kwargs: dict | None = None,
    progress: HomeTaskProgress | None = None,
) -> HomeDailyResult:
    """Compose the verified room tasks with one entry and one final exit."""
    progress = progress if progress is not None else HomeTaskProgress()
    if progress.life_bits_reason is not None:
        log("[HomeDaily] life bits already confirmed in this queue; resuming unfinished phase")
        life_result = HomeLifeBitsResult(True, "RESUME", progress.life_bits_reason)
    else:
        life_result = life_bits_runner(session, capture=capture, log=log)
        if life_result.success:
            progress.mark_life_bits(life_result.reason)
    if not life_result.success:
        return HomeDailyResult(False, "life_bits_failed", life_result.reason)
    if life_result.reason == "already_running":
        log("[HomeDaily] life bits already running; no restart needed")
    else:
        log("[HomeDaily] life bits completed; 8-hour task running")

    visit_kwargs = dict(random_visit_kwargs or {})
    visit_kwargs["exit_to_home"] = False
    visit_kwargs["progress"] = progress
    visit_kwargs.setdefault("home_page_detector", page_detector)
    visit_result = random_visit_runner(
        session,
        capture=capture,
        log=log,
        **visit_kwargs,
    )
    if not visit_result.success or visit_result.final_snapshot is None:
        return HomeDailyResult(
            False,
            "random_visit_failed",
            life_result.reason,
            visit_result.reason,
        )

    returned = visit_result.final_snapshot
    already_home = returned.state in {
        RandomVisitState.HOME_P1, RandomVisitState.HOME_P2, RandomVisitState.HOME_P3,
    }
    exited = returned if already_home else exit_home_room(
        session,
        returned,
        detect_state=detect_random_visit_state,
        state_of=lambda value: value.state,
        room_state=RandomVisitState.ROOM_MAIN,
        page_detector=page_detector,
        capture=capture,
        failure_state="QJNN_HomeDailyExitFailed",
        log=log,
        log_prefix="HomeDaily",
    )
    if exited is None:
        return HomeDailyResult(
            False,
            "exit_failed",
            life_result.reason,
            visit_result.reason,
        )

    navigation = navigator(
        "P2",
        session.capture_frame,
        session.click,
        detect_page=page_detector,
        sleep=session.sleep,
        log=log,
    )
    if not bool(getattr(navigation, "success", False)):
        if capture is not None:
            capture("QJNN_HomeDailyNavigateP2Failed", session.capture_frame())
        return HomeDailyResult(
            False,
            "navigate_p2_failed",
            life_result.reason,
            visit_result.reason,
        )
    if str(getattr(navigation, "page", "Unknown")) != "P2":
        if capture is not None:
            capture("QJNN_HomeDailyP2NotConfirmed", session.capture_frame())
        return HomeDailyResult(
            False,
            "p2_not_confirmed",
            life_result.reason,
            visit_result.reason,
        )
    log("[HomeDaily] completed; HOME P2 confirmed")
    return HomeDailyResult(
        True,
        "completed",
        life_result.reason,
        visit_result.reason,
    )


class HomeDailyAction(CustomAction):
    def run(self, context, argv) -> bool:
        session = None
        progress = None
        capture = DiagnosticFrameStore(HOME_DAILY_DIAGNOSTIC_DIR)
        try:
            session = _make_runtime_session(context)
            # Each action invocation may belong to a different game account.
            progress = HomeTaskProgress()
            self.allow_back_to_home_recovery = True
            result = run_home_daily(session, capture=capture.capture, progress=progress)
            self.allow_back_to_home_recovery = not progress.visit_pending
            print(
                f"[HomeDaily] result={result.reason}, "
                f"life_bits={result.life_bits_reason}, "
                f"random_visit={result.random_visit_reason}"
            )
            return result.success
        except Exception as exc:
            self.allow_back_to_home_recovery = False
            print(f"[HomeDaily] SafeStop exception: {exc}")
            if progress is not None:
                try:
                    print(f"[HomeDaily] confirmed visits={progress.successful_visits}/5; "
                          f"visit_pending={progress.visit_pending}; no uncertain input replay")
                except Exception as progress_exc:
                    print(f"[HomeDaily] progress unavailable: {progress_exc}")
            if session is not None:
                try:
                    capture.capture("QJNN_HomeDailyException", session.capture_frame())
                except Exception as capture_exc:
                    print(f"[HomeDaily] diagnostic unavailable: {capture_exc}")
                    print("[HomeDaily] device screenshot unavailable after failure; "
                          "check game/emulator/device transport; no app stop or restart requested")
            return False

"""Wait for a confirmed game update without interacting with loading screens."""
from __future__ import annotations

import time
from typing import Callable

from maa.custom_action import CustomAction
from maa.tasker import Tasker

from qjnn_foreground import focused_package
from qjnn_privacy import THIRD_PARTY_PACKAGES


GAME_PACKAGE = "com.tencent.tmgp.qjnn"
READY_GATES = (
    "QJNN_LoginScreenGate",
    "QJNN_StartButtonTemplateGate",
    "QJNN_EnterGameScreenTemplateGate",
    "QJNN_HomeScreenTemplateGate",
    "QJNN_InGamePage",
)


def wait_for_update(
    context,
    *,
    timeout_seconds: float = 600,
    interval_seconds: float = 1,
    package_resolver: Callable = focused_package,
    monotonic: Callable = time.monotonic,
    sleep: Callable = time.sleep,
) -> str | None:
    controller = context.tasker.controller
    deadline = monotonic() + timeout_seconds
    previous_gate = None
    stable_since = None
    while monotonic() < deadline and not context.tasker.stopping:
        package = package_resolver(controller)
        if package in THIRD_PARTY_PACKAGES:
            return "QJNN_PrivateLoginForeground"
        gate = None
        if package == GAME_PACKAGE:
            captured = controller.post_screencap().wait().succeeded
            frame = controller.cached_image if captured else None
            if frame is not None and getattr(frame, "size", 0):
                progress = context.run_recognition("QJNN_UpdateProgressText", frame)
                if progress is not None and progress.hit:
                    # A dimmed login/start screen underneath the dialog must
                    # never complete this wait, even if its template matches.
                    previous_gate = stable_since = None
                elif progress is not None:
                    for candidate in READY_GATES:
                        detail = context.run_recognition(
                            candidate, frame,
                        )
                        if detail is not None and detail.hit:
                            gate = candidate
                            break
        if context.tasker.stopping:
            return None
        if gate is not None and gate == previous_gate:
            if monotonic() - stable_since >= 2:
                return gate
        else:
            previous_gate = gate
            stable_since = monotonic() if gate is not None else None
        remaining = deadline - monotonic()
        if remaining > 0:
            sleep(min(interval_seconds, remaining))
    return None


class StartupUpdateWaitAction(CustomAction):
    # A slow/failed update must never trigger navigation or a task restart.
    allow_back_to_home_recovery = False

    def run(self, context, argv: CustomAction.RunArg) -> bool:
        # An update can restart the game or reveal a third-party login page.
        if not Tasker.set_save_on_error(False):
            return False
        print("已确认版本更新，等待下载和更新完成（最多 10 分钟）。")
        try:
            ready = wait_for_update(context)
        except Exception:
            print("版本更新等待失败，任务已停止。")
            return False
        if ready is None:
            print("版本更新等待超时或已停止，未继续后续任务。")
            return False
        if ready != "QJNN_PrivateLoginForeground":
            if not Tasker.set_save_on_error(True):
                return False
        return context.override_next(argv.node_name, [ready])

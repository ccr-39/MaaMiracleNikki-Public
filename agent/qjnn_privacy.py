from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition
from maa.tasker import Tasker

from qjnn_foreground import focused_package, launcher_package
from qjnn_login import LoginTextReader, login_screen_evidence
from app_paths import RESOURCE_ROOT


PROJECT_ROOT = RESOURCE_ROOT
TEMPLATE_ROOT = PROJECT_ROOT / "assets" / "resource" / "image"
TEMPLATE_REFERENCE_SHORT_SIDE = 900
GAME_PACKAGE = "com.tencent.tmgp.qjnn"

# These templates contain only game UI. The home template is the existing
# sign-in entry; it matches all currently registered home screenshots without
# inspecting any page text.
SAFE_GAME_TEMPLATES = (
    ("QJNN_StartButton", "start/start_button.png", 0.85),
    ("QJNN_EnterGameScreen", "start/enter_game_button.png", 0.85),
    ("QJNN_HomeScreen", "daily/signin_entry.png", 0.87),
)
THIRD_PARTY_PACKAGES = (
    "com.tencent.mobileqq",
    "com.tencent.mm",
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",
    "com.android.packageinstaller",
    "com.google.android.packageinstaller",
)


def _load_template(path: Path):
    import cv2
    import numpy as np

    data = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError("安全游戏模板无法读取")
    return image


def _match_score(frame, template) -> float:
    import cv2

    frame_height, frame_width = frame.shape[:2]
    frame_short_side = min(frame_height, frame_width)
    normalized_scale = frame_short_side / TEMPLATE_REFERENCE_SHORT_SIDE
    best_score = -1.0

    # Existing templates were cropped from 900x1600 samples, while the current
    # Maa controller supplies 720x1280 frames. Try both the source size and the
    # deterministic short-side-normalized size; no page text is inspected.
    for scale in dict.fromkeys((1.0, normalized_scale)):
        candidate = template
        if abs(scale - 1.0) > 1e-6:
            candidate = cv2.resize(
                template,
                None,
                fx=scale,
                fy=scale,
                interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC,
            )
        template_height, template_width = candidate.shape[:2]
        if template_width > frame_width or template_height > frame_height:
            continue
        scores = cv2.matchTemplate(frame, candidate, cv2.TM_CCOEFF_NORMED)
        best_score = max(best_score, float(cv2.minMaxLoc(scores)[1]))
    return best_score


def _matches(frame, template, threshold: float) -> bool:
    return _match_score(frame, template) >= threshold


def wait_for_safe_game_screen(
    controller,
    *,
    timeout_seconds: float,
    interval_seconds: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    cancelled: Callable[[], bool] = lambda: False,
    package_resolver: Callable | None = None,
    game_package: str = GAME_PACKAGE,
) -> str | None:
    """Poll in memory with game templates only; return the safe Pipeline node."""
    if cancelled():
        return None
    package_resolver = package_resolver or focused_package
    safe_templates = [
        (node_name, _load_template(TEMPLATE_ROOT / relative_path), threshold)
        for node_name, relative_path, threshold in SAFE_GAME_TEMPLATES
    ]
    deadline = monotonic() + timeout_seconds
    started_at = monotonic()
    privacy_transition_reported = False

    def pause(seconds):
        end = min(deadline, monotonic() + seconds)
        while not cancelled() and monotonic() < end:
            sleep(min(0.05, end - monotonic()))

    while monotonic() < deadline:
        if cancelled():
            return None
        job = controller.post_screencap().wait()
        if cancelled():
            return None
        frame = controller.cached_image
        if not job.succeeded or frame is None or getattr(frame, "size", 0) == 0:
            pause(interval_seconds)
            continue

        current_package = package_resolver(controller)
        if cancelled():
            return None
        for node_name, template, threshold in safe_templates:
            if current_package != game_package:
                break
            # After clicking the start button, its outgoing animation can leave
            # the same template visible briefly. Do not immediately route back
            # and hit the node's one-click limit.
            if node_name == "QJNN_StartButton" and monotonic() - started_at < 3.0:
                continue
            if not cancelled() and _matches(frame, template, threshold) and not cancelled():
                return node_name

        # Report a third-party transition only when its foreground package is
        # positively identified. Unknown/loading frames are never evidence of
        # a privacy transition.
        if not privacy_transition_reported:
            known_third_party = bool(
                current_package
                and current_package in THIRD_PARTY_PACKAGES
            )
            if known_third_party:
                print("已进入第三方登录隐私等待；仅执行内存中的游戏模板匹配。")
                privacy_transition_reported = True

        remaining = deadline - monotonic()
        if remaining > 0:
            pause(min(interval_seconds, remaining))

    return None


class LoginPrivacyGuard:
    """Keep protected/unknown foreground frames out of the task controller.

    The native controller is used only for in-memory login polling, avoiding
    re-entry into the outer controller's active screenshot callback.
    """

    def __init__(self, foreground_controller, *, cancelled=lambda: False,
                 game_package=GAME_PACKAGE):
        self.foreground_controller = foreground_controller
        self.cancelled = cancelled
        self.game_package = game_package
        self._login_text_reader = LoginTextReader()

    def _package(self):
        if self.cancelled():
            return None
        try:
            return focused_package(self.foreground_controller)
        except Exception:
            # Never print dumpsys output or exception details from this gate.
            return None

    def allow_input(self, method, native=None):
        package = self._package()
        if package == self.game_package and native is not None:
            # A game-owned login page can appear without changing packages.
            # Never wait and then replay coordinates recognized before login.
            allowed = self.capture(native, wait_for_login=False) is not None
        else:
            allowed = bool(package and package not in THIRD_PARTY_PACKAGES
                           and method == "post_start_app"
                           and package == launcher_package(self.foreground_controller))
        allowed = allowed and not self.cancelled()
        if not allowed:
            Tasker.set_save_on_error(False)
        return allowed

    def capture(self, native, *, wait_for_login=True):
        # Disable framework error captures before obtaining any unknown frame.
        if self.cancelled() or not Tasker.set_save_on_error(False):
            return None
        package = self._package()
        if not package or self.cancelled():
            return None
        if package in THIRD_PARTY_PACKAGES:
            return self._wait(native) if wait_for_login else None
        # Preserve the existing desktop -> StartApp route, but never treat an
        # arbitrary app (or an unresolved foreground) as a safe game screen.
        if package != self.game_package:
            if package != launcher_package(self.foreground_controller) or self.cancelled():
                return None
        job = native.post_screencap().wait()
        if self.cancelled():
            return None
        if not job.succeeded:
            raise RuntimeError("native screenshot failed")
        frame = native.cached_image
        if frame is None or getattr(frame, "size", 0) == 0:
            raise RuntimeError("native screenshot empty")
        current = self._package()
        if self.cancelled() or not current:
            return None
        if current in THIRD_PARTY_PACKAGES:
            return self._wait(native) if wait_for_login else None
        if current != package:
            return None
        if current == self.game_package:
            try:
                evidence = login_screen_evidence(frame, self._login_text_reader)
            except Exception:
                # An OCR/model failure cannot authorize input or release a frame.
                return None
            if evidence is not False:
                return self._wait(native) if wait_for_login else None
            if self.cancelled() or self._package() != current:
                return None
        if self.cancelled() or not Tasker.set_save_on_error(True):
            return None
        if self.cancelled():
            Tasker.set_save_on_error(False)
            return None
        return frame

    def _wait(self, native):
        print("第三方登录隐私保护已启用；请手动完成登录。")
        try:
            safe_node = wait_for_safe_game_screen(
                native, timeout_seconds=300, interval_seconds=1,
                cancelled=self.cancelled,
                package_resolver=lambda _: self._package(),
                game_package=self.game_package,
            )
        except Exception:
            print("隐私等待发生内部错误，未保存隐私页面截图。")
            return None
        if safe_node is None or self.cancelled():
            return None
        if self._package() != self.game_package or self.cancelled():
            return None
        if not Tasker.set_save_on_error(True):
            return None
        if self.cancelled():
            Tasker.set_save_on_error(False)
            return None
        return native.cached_image


class PrivateLoginWaitAction(CustomAction):
    """Shield third-party login frames from OCR, logs, and screenshot files."""

    def run(self, context, argv: CustomAction.RunArg) -> bool:
        cancelled = lambda: bool(context.tasker.stopping)
        # Also suppress error captures when stopping before this callback runs.
        if not Tasker.set_save_on_error(False):
            print("无法关闭错误截图，已在获取第三方登录画面前停止。")
            return False
        if cancelled():
            return False
        try:
            param = json.loads(argv.custom_action_param or "{}")
            timeout_seconds = max(1.0, float(param.get("timeout_ms", 300000)) / 1000)
            interval_seconds = max(0.2, float(param.get("interval_ms", 1000)) / 1000)
        except (TypeError, ValueError, json.JSONDecodeError):
            print("隐私等待参数无效，未获取或保存登录页面。")
            return False

        print("第三方登录隐私保护已启用；错误截图与全屏 OCR 已关闭。")
        try:
            safe_node = wait_for_safe_game_screen(
                context.tasker.controller,
                timeout_seconds=timeout_seconds,
                interval_seconds=interval_seconds,
                cancelled=cancelled,
            )
        except Exception:
            print("隐私等待发生内部错误，未保存隐私页面截图。")
            return False

        if cancelled():
            print("隐私等待已停止，未继续截图或任务。")
            return False
        if safe_node is None:
            print("登录等待超时，未保存隐私页面截图。")
            return False

        # The cached frame has just matched a non-sensitive game template, so
        # subsequent Pipeline OCR/logging and error screenshots are safe again.
        if not Tasker.set_save_on_error(True):
            print("已返回游戏界面，但无法恢复错误截图设置；任务安全停止。")
            return False
        if cancelled():
            Tasker.set_save_on_error(False)
            return False
        print(f"已返回安全游戏界面（{safe_node}），恢复正常识别与调试。")
        return True


class PrivateLoginForegroundRecognition(CustomRecognition):
    """Detect known third-party login packages without examining the frame."""

    def __init__(self, package_resolver: Callable = focused_package) -> None:
        super().__init__()
        self._package_resolver = package_resolver

    def analyze(self, context, argv: CustomRecognition.AnalyzeArg):
        current_package = self._package_resolver(context.tasker.controller)
        matched = bool(
            current_package
            and current_package in THIRD_PARTY_PACKAGES
        )
        return CustomRecognition.AnalyzeResult(
            box=[0, 0, 1, 1] if matched else None,
            detail={
                "focused_package": current_package,
                "private_login_foreground": matched,
            },
        )

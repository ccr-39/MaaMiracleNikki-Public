from __future__ import annotations


import argparse
import math
import re
import shutil
import sys
import time
import traceback
from pathlib import Path

from adb_controller import AdbController as AdbDevice, AdbError
from device_manager import create_controller
from qjnn_recognition import (
    ScreenDetection,
    ScreenState,
    TemplateMatchError,
    detect_screen,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from app_paths import DATA_ROOT
DEVICE_CONFIG = DATA_ROOT / "config" / "device.json"
TEMPLATE_ROOT = PROJECT_ROOT / "assets" / "resource" / "image"
SCREENSHOT_ROOT = DATA_ROOT / "images" / "screenshots"
DEBUG_ROOT = DATA_ROOT / "images" / "debug"
DEBUG_CURRENT_DIR = DEBUG_ROOT / "current"
DEBUG_UNKNOWN_DIR = DEBUG_ROOT / "unknown"
DEBUG_PIPELINE_DIR = DEBUG_ROOT / "pipeline"

PAGE_TIMEOUT = 8.0
CHECK_INTERVAL = 0.5
MAX_RETRY = 2
MAX_TOTAL_ACTIONS = 20
MAX_CLAIM_COUNT = 10

START_TEMPLATE = "start/start_button.png"
DAILY_TEMPLATES = (
    "home/home_marker.png",
    "home/daily_entry.png",
    "daily/daily_marker.png",
    "daily/claim_button.png",
    "common/reward_confirm.png",
)


def capture_name(value: str) -> str:
    name = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise argparse.ArgumentTypeError(
            "截图名称只能包含英文字母、数字、下划线和连字符"
        )
    try:
        capture_category(name)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return name


def capture_category(name: str) -> str:
    if (
        name in {"login", "enter_game"}
        or name.startswith("start")
        or name.startswith("login_")
        or name.startswith("enter_game_")
    ):
        return "start"
    if name.startswith("home"):
        return "home"
    if name.startswith("daily") or name == "reward_popup" or name.startswith("reward_"):
        return "daily"
    raise ValueError(
        "无法分类截图名称。请使用 start、login、enter_game、home、daily、daily_* "
        "或 reward_popup。"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="奇迹暖暖小助手（Miracle Nikki Assistant）固定页面 Pipeline 测试")
    parser.add_argument(
        "socket_id",
        nargs="?",
        help="MaaFramework 启动 Agent 时传入的 socket id",
    )
    parser.add_argument(
        "--task",
        choices=("start", "daily"),
        default="start",
        help="旧独立 start 已禁用；请使用 tools/run_task.py QJNN_StartAndEnterHome",
    )
    parser.add_argument(
        "--capture",
        type=capture_name,
        metavar="NAME",
        help="旧截图采集已因登录隐私保护禁用，不会连接设备或保存截图",
    )
    parser.add_argument(
        "--select-device",
        action="store_true",
        help="忽略已保存配置并重新选择设备",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.85,
        help="模板匹配阈值，默认 0.85",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="发生异常时输出完整 traceback",
    )
    return parser


def run_agent(socket_id: str) -> int:
    try:
        from maa.agent.agent_server import AgentServer
        from maa.toolkit import Toolkit
        import qjnn_action  # noqa: F401
        import qjnn_recognition  # noqa: F401
    except ImportError as exc:
        print(f"启动 Agent 失败：缺少 MaaFramework Python binding（{exc}）。")
        return 1

    Toolkit.init_option(str(DATA_ROOT))
    AgentServer.start_up(socket_id)
    AgentServer.join()
    AgentServer.shut_down()
    return 0


def capture_and_detect(
    device: AdbDevice,
    destination: Path,
    threshold: float,
) -> ScreenDetection:
    device.screenshot(destination)
    DEBUG_CURRENT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(destination, DEBUG_CURRENT_DIR / "current.png")
    return detect_screen(destination, TEMPLATE_ROOT, threshold)


def display_path(path: Path) -> Path:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return path.resolve()


def wait_for_screen(
    device: AdbDevice,
    target_state: ScreenState,
    *,
    threshold: float,
    timeout: float = PAGE_TIMEOUT,
    interval: float = CHECK_INTERVAL,
    screenshot_path: Path | None = None,
) -> tuple[bool, ScreenDetection, Path]:
    """Poll screenshots for a target state with a strict, finite check count."""
    if timeout <= 0 or interval <= 0:
        raise ValueError("timeout 和 interval 必须大于 0")
    destination = screenshot_path or DEBUG_PIPELINE_DIR / f"wait_{target_state.value.lower()}.png"
    deadline = time.monotonic() + timeout
    max_checks = max(1, math.ceil(timeout / interval) + 1)
    latest = ScreenDetection(ScreenState.UNKNOWN, 0.0, None, None)

    for check_index in range(max_checks):
        latest = capture_and_detect(device, destination, threshold)
        if latest.state is target_state:
            return True, latest, destination
        remaining = deadline - time.monotonic()
        if remaining <= 0 or check_index == max_checks - 1:
            break
        time.sleep(min(interval, remaining))
    return False, latest, destination


def save_unknown_snapshot(source: Path, stage: str) -> Path:
    DEBUG_UNKNOWN_DIR.mkdir(parents=True, exist_ok=True)
    for index in range(1, 1000):
        destination = DEBUG_UNKNOWN_DIR / f"unknown_{index:03d}.png"
        if not destination.exists():
            shutil.copy2(source, destination)
            print(f"[Pipeline] UNKNOWN stage: {stage}")
            print(f"[Pipeline] Saved: {display_path(destination)}")
            return destination
    raise OSError("unknown 截图编号已用尽，请整理 debug 目录")


def log_detection(detection: ScreenDetection, target: ScreenState | None = None) -> None:
    print("\n[Pipeline]")
    print(f"Current: {detection.state.value}")
    print(f"Recognition: {detection.template or 'none'}")
    print(f"Confidence: {detection.confidence:.4f}")
    if detection.match is not None:
        match = detection.match
        print(f"Match: ({match.x}, {match.y}, {match.width}, {match.height})")
        print(f"Center: ({match.center_x}, {match.center_y})")
    if target is not None:
        print(f"Target: {target.value}")


def run_start_pipeline(device: AdbDevice, threshold: float) -> int:
    missing = [
        name
        for name in (START_TEMPLATE,)
        if not (TEMPLATE_ROOT / name).is_file()
    ]
    if missing:
        print("Start pipeline 无法安全运行，缺少页面确认模板：")
        for name in missing:
            print(f"- assets/resource/image/{name}")
        return 1

    total_actions = 0
    for attempt in range(1, MAX_RETRY + 1):
        before = DEBUG_PIPELINE_DIR / "start_before.png"
        detection = capture_and_detect(device, before, threshold)
        log_detection(detection, ScreenState.LOGIN_SCREEN)

        if detection.state is ScreenState.LOGIN_SCREEN:
            print("Reached: LOGIN_SCREEN")
            return 0
        if detection.state is ScreenState.UNKNOWN:
            save_unknown_snapshot(before, f"START_SCREEN attempt {attempt}")
            print("UNKNOWN 页面不会执行点击，Pipeline 已停止。")
            return 1
        if detection.state is not ScreenState.START_SCREEN or detection.match is None:
            print(f"当前页面不是 START_SCREEN，停止任务：{detection.state.value}")
            return 1
        if total_actions >= MAX_TOTAL_ACTIONS:
            print(f"达到动作上限 {MAX_TOTAL_ACTIONS}，Pipeline 已停止。")
            return 1

        print("Action: Click")
        print("Target: LOGIN_SCREEN")
        device.click(detection.match.center_x, detection.match.center_y)
        total_actions += 1
        print("执行点击：成功")
        print("Waiting for LOGIN_SCREEN...")

        reached, latest, after = wait_for_screen(
            device,
            ScreenState.LOGIN_SCREEN,
            threshold=threshold,
            screenshot_path=DEBUG_PIPELINE_DIR / "start_after.png",
        )
        if reached:
            print("Reached: LOGIN_SCREEN")
            print(f"Confidence: {latest.confidence:.4f}")
            print(f"截图成功：{display_path(after)}")
            return 0

        print("Timeout waiting for LOGIN_SCREEN")
        if latest.state is ScreenState.UNKNOWN:
            save_unknown_snapshot(after, f"waiting LOGIN_SCREEN attempt {attempt}")
            print("UNKNOWN 页面不会执行点击，Pipeline 已停止。")
            return 1
        if latest.state is not ScreenState.START_SCREEN:
            print(f"等待期间进入非目标页面：{latest.state.value}，Pipeline 已停止。")
            return 1
        if attempt < MAX_RETRY:
            print(f"仍处于 START_SCREEN，将进行最后一次重试（{attempt + 1}/{MAX_RETRY}）。")

    print(f"启动页 Pipeline 在 {MAX_RETRY} 次有限尝试后失败。")
    return 1


def run_daily_pipeline() -> int:
    missing = [name for name in DAILY_TEMPLATES if not (TEMPLATE_ROOT / name).is_file()]
    print("Daily pipeline 尚未配置完成。")
    print("预留流程：HOME_SCREEN → DAILY_SCREEN → CLAIM → REWARD_POPUP → DONE")
    print(
        f"安全限制：MAX_TOTAL_ACTIONS={MAX_TOTAL_ACTIONS}, MAX_RETRY={MAX_RETRY}, "
        f"PAGE_TIMEOUT={PAGE_TIMEOUT:g}, MAX_CLAIM_COUNT={MAX_CLAIM_COUNT}"
    )
    if missing:
        print("缺少模板：")
        for name in missing:
            print(f"- assets/resource/image/{name}")
    else:
        print("模板文件已存在，但尚未用真实页面校准状态与跳转，因此不会执行动作。")
    return 0


def next_capture_path(name: str) -> Path:
    destination_dir = SCREENSHOT_ROOT / capture_category(name)
    destination_dir.mkdir(parents=True, exist_ok=True)
    for index in range(1, 10000):
        candidate = destination_dir / f"{name}_{index:03d}.png"
        if not candidate.exists():
            return candidate
    raise OSError(f"截图名称 {name!r} 的编号已用尽，请整理 images/screenshots 目录")


def run_capture(device: AdbDevice, name: str) -> int:
    destination = next_capture_path(name)
    device.screenshot(destination)
    print(f"截图保存成功：{display_path(destination)}")
    return 0


def run_standalone(
    *,
    task: str,
    capture: str | None,
    force_select: bool,
    threshold: float,
) -> int:
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("--threshold 必须在 0 到 1 之间")
    # The legacy device API writes captures before recognition and cannot use
    # LoginPrivacyGuard's native in-memory capture boundary. Fail before device
    # selection, including explicit --capture, rather than run an unguarded path.
    if capture is not None or task == "start":
        print(
            "旧独立启动和截图采集入口已因登录隐私保护禁用，未连接设备或保存截图。"
            "请使用正式 CLI：python tools/run_task.py QJNN_StartAndEnterHome",
            file=sys.stderr,
        )
        return 2
    return run_daily_pipeline()


def main() -> int:
    args = build_parser().parse_args()
    if args.socket_id:
        if args.select_device or args.capture is not None:
            print("Agent socket 模式不支持 --select-device 或 --capture。")
            return 2
        return run_agent(args.socket_id)

    try:
        return run_standalone(
            task=args.task,
            capture=args.capture,
            force_select=args.select_device,
            threshold=args.threshold,
        )
    except (AdbError, TemplateMatchError, OSError, ValueError) as exc:
        print(f"错误：{exc}")
        if args.debug:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())

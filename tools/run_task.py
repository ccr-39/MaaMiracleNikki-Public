from __future__ import annotations


import argparse
import gc
import json
import sys
from pathlib import Path
from threading import Event


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app_paths import DATA_ROOT
AGENT_DIR = ROOT / "agent"
RESOURCE_DIR = ROOT / "assets" / "resource"
INTERFACE_PATH = ROOT / "assets" / "interface.json"
LOG_DIR = DATA_ROOT / "images" / "debug" / "pipeline"

# Current-screen diagnostics must preserve the operator-provided screen. Every
# other interface task gets the preflight by default, including future formal
# tasks, so the shared popup layer cannot be silently omitted.
COMMON_POPUP_PREFLIGHT_EXEMPT_TASKS = frozenset(
    {
        "QJNN_ConnectionTest",
        "QJNN_StartGame",
        "QJNN_SigninRewardDebug",
        "QJNN_AllianceCommissionSettlementDebug",
        "QJNN_AllianceAfterCommissionDebug",
        "QJNN_ArenaExitDebug",
        "QJNN_CompetitionExitDebug",
        "QJNN_DreamweaverCommissionReadOnlyDebug",
        "QJNN_CommonPopupDebug",
        "QJNN_FriendHomeAnyPageDebug",
        "QJNN_FriendBackDebug",
    }
)
COMMON_POPUP_RUNTIME_JUMPBACK = "[JumpBack]QJNN_CommonPopupRuntime"
COMMON_POPUP_RUNTIME_EXEMPT_NODES = frozenset(
    {
        "QJNN_CommonAchievementTitleOcr",
        "QJNN_CommonPopupRuntime",
        "QJNN_CommonPopupPreflight",
        "QJNN_CommonPopupDebug",
        # Do not add OCR/capture work inside the protected third-party login
        # wait. The surrounding startup pipeline remains guarded.
        "QJNN_PrivateLoginWait",
        "QJNN_UpdateWait",
    }
)
RUNTIME_CUSTOM_RECOGNITION_NAMES = frozenset(
    {
        "qjnn_home_any_page",
        "qjnn_package_foreground",
        "qjnn_private_login_foreground",
        "qjnn_login_screen",
        "qjnn_reward_clothes_card",
        "qjnn_reward_suit_showcase",
        "qjnn_signin_state",
        "qjnn_limited_signin_page",
        "qjnn_friend_send_heart_result",
        "qjnn_clothes_shop_state",
        "qjnn_back_to_home_unknown",
    }
)
RUNTIME_CUSTOM_ACTION_NAMES = frozenset(
    {
        "qjnn_save_signin_failure",
        "qjnn_signin_makeup_once",
        "qjnn_limited_signin",
        "qjnn_save_mail_failure",
        "qjnn_mail_rewards",
        "qjnn_friend_mail",
        "qjnn_private_login_wait",
        "qjnn_startup_update_wait",
        "qjnn_home_navigate",
        "qjnn_home_to_p3",
        "qjnn_home_daily",
        "qjnn_home_life_bits",
        "qjnn_home_random_visit",
        "qjnn_alliance_commission",
        "qjnn_alliance_commission_settlement_debug",
        "qjnn_alliance_commission_map_exit",
        "qjnn_alliance_daily_exit",
        "qjnn_alliance_daily",
        "qjnn_library",
        "qjnn_mystery_house_daily",
        "qjnn_world_tour",
        "qjnn_arena_daily",
        "qjnn_arena_exit_debug",
        "qjnn_competition_daily",
        "qjnn_competition_exit_debug",
        "qjnn_clothes_shop_purchase",
        "qjnn_decompose_daily",
        "qjnn_dreamweaver_commission",
        "qjnn_stamina_daily",
        "qjnn_daily_task_claim",
        "qjnn_dreamweaver_commission_read_only",
        "qjnn_common_popup_debug",
        "qjnn_common_popup_preflight",
        "qjnn_common_popup_runtime",
        "qjnn_friend_send_heart_click",
        "qjnn_friend_send_heart_wait_result",
        "qjnn_friend_back",
        "qjnn_back_to_home_recovery",
        "qjnn_back_to_home_after_failure",
    }
)


def _pipeline_extension_names(kind: str) -> frozenset[str]:
    names: set[str] = set()
    discriminator = "recognition" if kind == "recognition" else "action"
    expected = "Custom"
    name_field = f"custom_{kind}"

    def walk(value) -> None:
        if isinstance(value, dict):
            if value.get(discriminator) == expected:
                name = value.get(name_field)
                if isinstance(name, str) and name:
                    names.add(name)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for path in sorted((RESOURCE_DIR / "pipeline").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        walk(payload)
    return frozenset(names)


def pipeline_custom_recognition_names() -> frozenset[str]:
    return _pipeline_extension_names("recognition")


def pipeline_custom_action_names() -> frozenset[str]:
    return _pipeline_extension_names("action")


def validate_custom_recognition_runtime_coverage() -> tuple[frozenset[str], frozenset[str]]:
    referenced = pipeline_custom_recognition_names()
    missing = referenced - RUNTIME_CUSTOM_RECOGNITION_NAMES
    if missing:
        raise RuntimeError(
            "Pipeline 引用了未在正式 Resource runtime 注册的 CustomRecognition："
            + ", ".join(sorted(missing))
        )
    return referenced, RUNTIME_CUSTOM_RECOGNITION_NAMES


def validate_custom_action_runtime_coverage() -> tuple[frozenset[str], frozenset[str]]:
    referenced = pipeline_custom_action_names()
    missing = referenced - RUNTIME_CUSTOM_ACTION_NAMES
    if missing:
        raise RuntimeError(
            "Pipeline 引用了未在正式 Resource runtime 注册的 CustomAction："
            + ", ".join(sorted(missing))
        )
    return referenced, RUNTIME_CUSTOM_ACTION_NAMES


def should_run_common_popup_preflight(task_name: str) -> bool:
    return task_name not in COMMON_POPUP_PREFLIGHT_EXEMPT_TASKS


def build_common_popup_runtime_override() -> dict[str, dict]:
    """Append the transient fallback after each node's business candidates."""
    override: dict[str, dict] = {}
    for path in sorted((RESOURCE_DIR / "pipeline").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            continue
        for node_name, node in payload.items():
            if (
                node_name in COMMON_POPUP_RUNTIME_EXEMPT_NODES
                or not isinstance(node, dict)
            ):
                continue
            next_list = node.get("next")
            if isinstance(next_list, str):
                next_list = [next_list]
            if not isinstance(next_list, list) or not next_list:
                continue
            if COMMON_POPUP_RUNTIME_JUMPBACK in next_list:
                continue
            guarded_next = list(next_list)
            safe_stop_index = next(
                (
                    index
                    for index, candidate in enumerate(guarded_next)
                    if candidate == "QJNN_SafeStop"
                    or (
                        isinstance(candidate, dict)
                        and candidate.get("name") == "QJNN_SafeStop"
                    )
                ),
                len(guarded_next),
            )
            guarded_next.insert(safe_stop_index, COMMON_POPUP_RUNTIME_JUMPBACK)
            override[node_name] = {"next": guarded_next}
    return override

if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))


def load_interface_tasks() -> dict[str, str]:
    """Return executable Pipeline entries and labels declared by interface.json."""
    try:
        interface = json.loads(INTERFACE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取任务配置 {INTERFACE_PATH}：{exc}") from exc

    tasks = interface.get("task", []) if isinstance(interface, dict) else []
    if not isinstance(tasks, list):
        raise RuntimeError("assets/interface.json 中的 task 必须是数组")

    available: dict[str, str] = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        entry = task.get("entry")
        name = task.get("name")
        if isinstance(entry, str) and entry:
            available[entry] = name if isinstance(name, str) and name else entry

    if not available:
        raise RuntimeError("assets/interface.json 中没有可执行任务入口")
    return available


def build_parser(available_tasks: dict[str, str]) -> argparse.ArgumentParser:
    task_help = "\n".join(
        f"  {task_id}: {label}" for task_id, label in available_tasks.items()
    )
    parser = argparse.ArgumentParser(
        description="运行正式 MaaFramework Pipeline 任务（不在 Python 中实现业务流程）",
        epilog="当前可用任务：\n" + task_help,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("task", metavar="TASK", help="assets/interface.json 中注册的 Pipeline task id")
    parser.add_argument(
        "--select-device",
        action="store_true",
        help="忽略已保存配置并重新选择设备",
    )
    parser.add_argument(
        "--device-serial",
        metavar="SERIAL",
        help="显式指定设备序列号，例如 127.0.0.1:<实例ADB端口>（将占位符替换为实际数字端口）；默认仍读取 config/device.json",
    )
    parser.add_argument(
        "--init-only",
        action="store_true",
        help="只初始化并绑定 Resource、Controller、Tasker，不提交任务",
    )
    parser.add_argument(
        "--capture-alliance",
        action="store_true",
        help="在已确认的联盟缺素材页面执行原动作前，各保存一张标准化截图",
    )
    parser.add_argument(
        "--capture-arena",
        action="store_true",
        help="在已确认的竞技场关键状态各保存一张标准化截图",
    )
    parser.add_argument(
        "--capture-competition",
        action="store_true",
        help="在已确认的搭配评选赛关键状态各保存一张标准化截图",
    )
    parser.add_argument(
        "--capture-common-popup",
        action="store_true",
        help="在安全处理已知公共弹窗前保存一次当前 Maa Controller 原帧",
    )
    return parser


def create_runtime_controller(
    *, force_select: bool, device_serial: str | None = None,
    auto_connect: bool | None = None,
    adb_path: str | Path | None = None, profile: str | None = None,
):
    """Create the one ADB path/serial context used by the formal task run."""
    from device_manager import create_controller

    connection_options = {}
    if adb_path is not None:
        connection_options["adb_path"] = adb_path
    if profile is not None:
        connection_options["profile"] = profile
    if auto_connect is None:
        return create_controller(device_serial="" if force_select else device_serial, **connection_options)
    return create_controller(
        device_serial=device_serial,
        allow_fallback=False,
        strict_serial=True,
        auto_connect=auto_connect,
        **connection_options,
    )


def register_local_extensions(
    resource,
    *,
    alliance_capture=None,
    arena_capture=None,
    competition_capture=None,
    common_popup_capture=None,
    run_state=None,
) -> bool:
    """Register required custom actions and recognitions on the Resource."""
    from qjnn_run_state import RunState
    if run_state is None:
        run_state = RunState()
    from qjnn_back_to_home import (
        RecoveryRuntime, RecoveryResource, RecoveryRecognition, RecoveryAction, QueueFailureReturnHomeAction,
    )
    recovery_runtime = RecoveryRuntime()
    resource._back_to_home_runtime = recovery_runtime
    recovery_registered = (
        resource.register_custom_recognition("qjnn_back_to_home_unknown", RecoveryRecognition(recovery_runtime))
        and resource.register_custom_action("qjnn_back_to_home_recovery", RecoveryAction(recovery_runtime))
        and resource.register_custom_action("qjnn_back_to_home_after_failure", QueueFailureReturnHomeAction(recovery_runtime))
    )
    resource = RecoveryResource(resource, recovery_runtime)
    from maa.custom_action import CustomAction
    from maa.custom_recognition import CustomRecognition
    from qjnn_alliance_commission import (
        AllianceCommissionAction,
        AllianceCommissionMapExitAction,
        AllianceCommissionSettlementDebugAction,
    )
    from qjnn_alliance_daily import AllianceDailyAction, AllianceDailyExitAction
    from qjnn_arena import ArenaDailyAction, ArenaExitDebugAction
    from qjnn_competition import CompetitionDailyAction, CompetitionExitDebugAction
    from qjnn_clothes_shop import ClothesShopPurchaseAction, ClothesShopStateRecognition
    from qjnn_decompose import DecomposeDailyAction
    from qjnn_daily_task_claim import DailyTaskClaimAction
    from qjnn_stamina_daily import StaminaDailyAction
    from qjnn_dreamweaver_commission import (
        DreamweaverCommissionAction,
        DreamweaverCommissionReadOnlyAction,
    )
    from qjnn_common_popup import (
        CommonPopupDebugAction,
        CommonPopupPreflightAction,
        CommonPopupRuntimeAction,
    )
    from qjnn_friend_send_heart import (
        FriendBackAction,
        FriendSendHeartResultRecognition,
        FriendSendHeartClickAction,
        FriendSendHeartWaitingResultAction,
        HomeAnyPageRecognition,
    )
    from qjnn_home_navigation import HomeNavigateAction
    from qjnn_home_daily import HomeDailyAction
    from qjnn_home_life_bits import HomeLifeBitsAction
    from qjnn_home_random_visit import HomeRandomVisitAction
    from qjnn_library import LibraryAction
    from qjnn_mystery_house import MysteryHouseDailyAction
    from qjnn_world_tour import WorldTourAction
    from qjnn_foreground import PackageForegroundRecognition
    from qjnn_privacy import PrivateLoginForegroundRecognition, PrivateLoginWaitAction
    from qjnn_login import LoginScreenRecognition
    from qjnn_startup_update import StartupUpdateWaitAction
    from qjnn_reward import MailRewardAction, RewardClothesRecognition, RewardSuitShowcaseRecognition
    from qjnn_signin import SigninStateRecognition
    from qjnn_friend_mail import FriendMailAction
    from qjnn_signin_makeup import SigninMakeupAction
    from qjnn_limited_signin import LimitedSigninAction, LimitedSigninPageRecognition

    class SaveSigninFailure(CustomAction):
        def run(self, context, argv: CustomAction.RunArg) -> bool:
            try:
                import cv2

                controller = context.tasker.controller
                if not controller.post_screencap().wait().succeeded:
                    return False
                image = controller.cached_image
                if image is None or getattr(image, "size", 0) == 0:
                    return False

                LOG_DIR.mkdir(parents=True, exist_ok=True)
                destination = next(
                    (
                        LOG_DIR / f"signin_failure_{index:03d}.png"
                        for index in range(1, 1000)
                        if not (LOG_DIR / f"signin_failure_{index:03d}.png").exists()
                    ),
                    None,
                )
                if destination is None:
                    return False
                encoded, data = cv2.imencode(".png", image)
                if not encoded:
                    return False
                data.tofile(str(destination))
                print(f"签到失败调试截图：{destination}")
                return True
            except Exception as exc:
                print(f"保存签到失败调试截图失败：{exc}")
                return False

    class SaveMailFailure(CustomAction):
        def run(self, context, argv: CustomAction.RunArg) -> bool:
            try:
                import cv2

                controller = context.tasker.controller
                if not controller.post_screencap().wait().succeeded:
                    return False
                image = controller.cached_image
                if image is None or getattr(image, "size", 0) == 0:
                    return False

                LOG_DIR.mkdir(parents=True, exist_ok=True)
                destination = next(
                    (
                        LOG_DIR / f"mail_failure_{index:03d}.png"
                        for index in range(1, 1000)
                        if not (LOG_DIR / f"mail_failure_{index:03d}.png").exists()
                    ),
                    None,
                )
                if destination is None:
                    return False
                encoded, data = cv2.imencode(".png", image)
                if not encoded:
                    return False
                data.tofile(str(destination))
                print(f"邮箱任务失败调试截图：{destination}")
                return True
            except Exception as exc:
                print(f"保存邮箱任务失败调试截图失败：{exc}")
                return False

    signin_action_registered = resource.register_custom_action(
        "qjnn_save_signin_failure", SaveSigninFailure()
    )
    mail_action_registered = resource.register_custom_action(
        "qjnn_save_mail_failure", SaveMailFailure()
    )
    private_login_action_registered = resource.register_custom_action(
        "qjnn_private_login_wait", PrivateLoginWaitAction()
    )
    startup_update_action_registered = resource.register_custom_action(
        "qjnn_startup_update_wait", StartupUpdateWaitAction()
    )
    home_navigate_action_registered = resource.register_custom_action(
        "qjnn_home_navigate", HomeNavigateAction()
    )
    home_to_p3_action_registered = resource.register_custom_action(
        "qjnn_home_to_p3", HomeNavigateAction()
    )
    home_daily_action_registered = resource.register_custom_action(
        "qjnn_home_daily", HomeDailyAction()
    )
    home_life_bits_action_registered = resource.register_custom_action(
        "qjnn_home_life_bits", HomeLifeBitsAction()
    )
    home_random_visit_action_registered = resource.register_custom_action(
        "qjnn_home_random_visit", HomeRandomVisitAction()
    )
    alliance_commission_action_registered = resource.register_custom_action(
        "qjnn_alliance_commission", AllianceCommissionAction(alliance_capture)
    )
    alliance_settlement_debug_action_registered = resource.register_custom_action(
        "qjnn_alliance_commission_settlement_debug",
        AllianceCommissionSettlementDebugAction(alliance_capture),
    )
    alliance_commission_map_exit_action_registered = resource.register_custom_action(
        "qjnn_alliance_commission_map_exit",
        AllianceCommissionMapExitAction(),
    )
    alliance_daily_exit_action_registered = resource.register_custom_action(
        "qjnn_alliance_daily_exit", AllianceDailyExitAction()
    )
    alliance_daily_action_registered = resource.register_custom_action(
        "qjnn_alliance_daily", AllianceDailyAction(alliance_capture)
    )
    library_action_registered = resource.register_custom_action(
        "qjnn_library", LibraryAction(alliance_capture)
    )
    mystery_house_action_registered = resource.register_custom_action(
        "qjnn_mystery_house_daily", MysteryHouseDailyAction(run_state)
    )
    world_tour_action_registered = resource.register_custom_action(
        "qjnn_world_tour", WorldTourAction()
    )
    arena_action_registered = resource.register_custom_action(
        "qjnn_arena_daily", ArenaDailyAction(arena_capture)
    )
    arena_exit_debug_action_registered = resource.register_custom_action(
        "qjnn_arena_exit_debug", ArenaExitDebugAction(arena_capture)
    )
    competition_action_registered = resource.register_custom_action(
        "qjnn_competition_daily", CompetitionDailyAction(competition_capture, run_state=run_state)
    )
    competition_exit_debug_action_registered = resource.register_custom_action(
        "qjnn_competition_exit_debug",
        CompetitionExitDebugAction(competition_capture),
    )
    clothes_shop_action_registered = resource.register_custom_action(
        "qjnn_clothes_shop_purchase", ClothesShopPurchaseAction(run_state)
    )
    decompose_action_registered = resource.register_custom_action(
        "qjnn_decompose_daily", DecomposeDailyAction(run_state)
    )
    daily_task_claim_action_registered = resource.register_custom_action(
        "qjnn_daily_task_claim", DailyTaskClaimAction()
    )
    stamina_daily_action_registered = resource.register_custom_action(
        "qjnn_stamina_daily", StaminaDailyAction(run_state)
    )
    dreamweaver_commission_action_registered = resource.register_custom_action(
        "qjnn_dreamweaver_commission", DreamweaverCommissionAction(run_state)
    )
    dreamweaver_commission_read_only_action_registered = resource.register_custom_action(
        "qjnn_dreamweaver_commission_read_only",
        DreamweaverCommissionReadOnlyAction(),
    )
    common_popup_debug_action_registered = resource.register_custom_action(
        "qjnn_common_popup_debug",
        CommonPopupDebugAction(common_popup_capture),
    )
    common_popup_preflight_action_registered = resource.register_custom_action(
        "qjnn_common_popup_preflight",
        CommonPopupPreflightAction(common_popup_capture),
    )
    common_popup_runtime_action_registered = resource.register_custom_action(
        "qjnn_common_popup_runtime",
        CommonPopupRuntimeAction(),
    )
    friend_send_heart_click_registered = resource.register_custom_action(
        "qjnn_friend_send_heart_click", FriendSendHeartClickAction()
    )
    friend_send_heart_wait_registered = resource.register_custom_action(
        "qjnn_friend_send_heart_wait_result",
        FriendSendHeartWaitingResultAction(),
    )
    friend_back_registered = resource.register_custom_action(
        "qjnn_friend_back", FriendBackAction()
    )
    recognition_registered = resource.register_custom_recognition(
        "qjnn_package_foreground", PackageForegroundRecognition()
    )
    private_login_recognition_registered = resource.register_custom_recognition(
        "qjnn_private_login_foreground", PrivateLoginForegroundRecognition()
    )
    login_screen_registered = resource.register_custom_recognition(
        "qjnn_login_screen", LoginScreenRecognition()
    )
    reward_clothes_recognition_registered = resource.register_custom_recognition(
        "qjnn_reward_clothes_card", RewardClothesRecognition()
    )
    reward_suit_recognition_registered = resource.register_custom_recognition(
        "qjnn_reward_suit_showcase", RewardSuitShowcaseRecognition()
    )
    home_any_page_recognition_registered = resource.register_custom_recognition(
        "qjnn_home_any_page", HomeAnyPageRecognition()
    )
    friend_result_recognition_registered = resource.register_custom_recognition(
        "qjnn_friend_send_heart_result", FriendSendHeartResultRecognition()
    )
    signin_state_recognition_registered = resource.register_custom_recognition(
        "qjnn_signin_state", SigninStateRecognition()
    )
    clothes_shop_state_recognition_registered = resource.register_custom_recognition(
        "qjnn_clothes_shop_state", ClothesShopStateRecognition()
    )
    friend_mail_registered = resource.register_custom_action("qjnn_friend_mail", FriendMailAction(run_state))
    mail_rewards_registered = resource.register_custom_action("qjnn_mail_rewards", MailRewardAction(run_state))
    signin_makeup_registered = resource.register_custom_action("qjnn_signin_makeup_once", SigninMakeupAction())
    limited_signin_registered = resource.register_custom_action("qjnn_limited_signin", LimitedSigninAction())
    limited_page_registered = resource.register_custom_recognition(
        "qjnn_limited_signin_page", LimitedSigninPageRecognition())
    return (
        recovery_registered
        and login_screen_registered
        and signin_makeup_registered
        and limited_signin_registered
        and limited_page_registered
        and friend_mail_registered
        and mail_rewards_registered
        and signin_action_registered
        and mail_action_registered
        and private_login_action_registered
        and startup_update_action_registered
        and home_navigate_action_registered
        and home_to_p3_action_registered
        and home_daily_action_registered
        and home_life_bits_action_registered
        and home_random_visit_action_registered
        and alliance_commission_action_registered
        and alliance_settlement_debug_action_registered
        and alliance_commission_map_exit_action_registered
        and alliance_daily_exit_action_registered
        and alliance_daily_action_registered
        and library_action_registered
        and mystery_house_action_registered
        and world_tour_action_registered
        and arena_action_registered
        and arena_exit_debug_action_registered
        and competition_action_registered
        and competition_exit_debug_action_registered
        and clothes_shop_action_registered
        and decompose_action_registered
        and dreamweaver_commission_action_registered
        and daily_task_claim_action_registered
        and stamina_daily_action_registered
        and dreamweaver_commission_read_only_action_registered
        and common_popup_debug_action_registered
        and common_popup_preflight_action_registered
        and common_popup_runtime_action_registered
        and friend_send_heart_click_registered
        and friend_send_heart_wait_registered
        and friend_back_registered
        and recognition_registered
        and private_login_recognition_registered
        and reward_clothes_recognition_registered
        and reward_suit_recognition_registered
        and home_any_page_recognition_registered
        and friend_result_recognition_registered
        and signin_state_recognition_registered
        and clothes_shop_state_recognition_registered
    )


class TaskRunStopped(Exception):
    """Optional desktop cancellation; existing CLI execution is unchanged."""


class TaskRuntimeInitError(RuntimeError):
    def __init__(self, phase, message):
        super().__init__(message)
        self.phase = phase


def _wait_task_result(tasker, job, *, stop_event: Event | None = None):
    if stop_event is None:
        return job.get(wait=True)
    while not job.done:
        if stop_event.wait(0.05):
            # Cooperatively stop via MaaFramework, then wait for callbacks to
            # unwind. Never kill a thread or change the business SafeStop node.
            tasker.post_stop().wait()
            job.wait()
            raise TaskRunStopped()
    if stop_event.is_set():
        tasker.post_stop().wait()
        raise TaskRunStopped()
    return job.get(wait=True)


class TaskRuntime:
    """One batch owns the existing runner's ADB/Resource/Controller/Tasker."""

    def __init__(self, **options):
        self.options = options
        self.runtime_controller = None
        self.resource = None
        self.controller = None
        self.tasker = None
        self._capture_configured = False
        self._run_state = None
        self._owns_run_state = True
        self._privacy_stop_signal = [None]

    def __enter__(self):
        try:
            self.open(**self.options)
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def open(
        self, *, force_select: bool = False, device_serial: str | None = None,
        auto_connect: bool | None = None, stop_event: Event | None = None,
        adb_path: str | Path | None = None, profile: str | None = None,
        capture_alliance: bool = False, capture_arena: bool = False,
        capture_competition: bool = False, capture_common_popup: bool = False,
        run_state=None,
    ):
        if self.resource is not None or self.tasker is not None:
            raise RuntimeError("运行环境已经初始化")
        self._privacy_stop_signal[0] = stop_event
        from qjnn_run_state import RunState
        self._run_state = run_state if run_state is not None else RunState()
        self._owns_run_state = run_state is None
        if stop_event is not None and stop_event.is_set():
            raise TaskRunStopped()
        from qjnn_common_popup import configure_common_popup_capture

        try:
            referenced_recognitions, registered_recognitions = (
                validate_custom_recognition_runtime_coverage()
            )
            referenced_actions, registered_actions = (
                validate_custom_action_runtime_coverage()
            )
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            print(f"错误：Custom extension runtime 配置无效：{exc}", file=sys.stderr)
            raise RuntimeError(str(exc)) from exc
        print(
            "[CustomRecognition] runtime coverage OK: "
            f"referenced={sorted(referenced_recognitions)}, "
            f"registered={sorted(registered_recognitions)}"
        )
        print(
            "[CustomAction] runtime coverage OK: "
            f"referenced={sorted(referenced_actions)}, "
            f"registered={sorted(registered_actions)}"
        )

        common_popup_capture = None
        if capture_common_popup:
            from qjnn_common_popup_capture import CommonPopupCaptureManager

            common_popup_capture = CommonPopupCaptureManager()
            print(
                "[CommonPopupCapture] enabled: "
                + str(common_popup_capture.output_dir.resolve())
            )
        configure_common_popup_capture(
            common_popup_capture.capture_once if common_popup_capture is not None else None
        )
        self._capture_configured = True
        alliance_capture = None
        if capture_alliance:
            from qjnn_alliance_capture import AllianceCaptureManager

            alliance_capture = AllianceCaptureManager()
            print(
                "[AllianceCapture] enabled: "
                + str(alliance_capture.output_dir.resolve())
            )
        arena_capture = None
        if capture_arena:
            from qjnn_arena_capture import ArenaCaptureManager

            arena_capture = ArenaCaptureManager()
            print("[ArenaCapture] enabled: " + str(arena_capture.output_dir.resolve()))
        competition_capture = None
        if capture_competition:
            from qjnn_competition_capture import CompetitionCaptureManager

            competition_capture = CompetitionCaptureManager()
            print(
                "[CompetitionCapture] enabled: "
                + str(competition_capture.output_dir.resolve())
            )

        if stop_event is not None and stop_event.is_set():
            raise TaskRunStopped()
        try:
            runtime_controller = create_runtime_controller(
                force_select=force_select,
                device_serial=device_serial,
                auto_connect=auto_connect,
                adb_path=adb_path,
                profile=profile,
            )
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            print(f"错误：ADB Controller 初始化失败：{exc}", file=sys.stderr)
            raise TaskRuntimeInitError("connection", str(exc)) from exc

        self.runtime_controller = runtime_controller
        if stop_event is not None and stop_event.is_set():
            raise TaskRunStopped()

        try:
            from maa_recovering_controller import RecoveringController
            from maa.library import Library
            from maa.resource import Resource
            from maa.tasker import Tasker
        except ImportError as exc:
            print(f"错误：缺少 MaaFramework Python binding：{exc}", file=sys.stderr)
            raise RuntimeError(str(exc)) from exc

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        Tasker.set_log_dir(LOG_DIR)
        Tasker.set_save_on_error(True)

        try:
            print(f"MaaFramework version: {Library.version()}")
            resource = Resource()
            self.resource = resource
            print("Resource 创建成功。")
            load_job = resource.post_bundle(RESOURCE_DIR).wait()
            if not load_job.succeeded or not resource.loaded:
                raise RuntimeError(f"MaaFramework 资源加载失败：{RESOURCE_DIR}")
            print(f"Resource 加载成功：{RESOURCE_DIR}")
            if not register_local_extensions(
                resource,
                alliance_capture=alliance_capture,
                arena_capture=arena_capture,
                competition_capture=competition_capture,
                common_popup_capture=common_popup_capture,
                run_state=self._run_state,
            ):
                raise RuntimeError("无法向 MaaFramework Resource 注册本地扩展")

            if stop_event is not None and stop_event.is_set():
                raise TaskRunStopped()
            print(f"\n当前设备：{runtime_controller.serial}")
            controller = RecoveringController(runtime_controller)
            self.controller = controller
            print("Controller 创建成功。")
            connection = controller.post_connection().wait()
            if not connection.succeeded or not controller.connected():
                raise RuntimeError(
                    f"MaaFramework Controller 无法连接设备：{runtime_controller.serial}"
                )
            print("Controller 连接成功。")
            if stop_event is not None and stop_event.is_set():
                raise TaskRunStopped()

            tasker = Tasker()
            self.tasker = tasker
            from types import SimpleNamespace
            from weakref import ref
            from qjnn_privacy import LoginPrivacyGuard

            tasker_ref = ref(tasker)
            controller_ref = ref(controller)
            privacy_stop_signal = self._privacy_stop_signal

            def privacy_cancelled():
                active = tasker_ref()
                signal = privacy_stop_signal[0]
                return active is None or bool(active.stopping) or bool(
                    signal is not None and signal.is_set())

            def privacy_controller_info():
                active = controller_ref()
                return active.get_custom_info() if active is not None else {}

            # The controller boundary precedes native OCR, custom callbacks,
            # error captures and queue recovery for every GUI/CLI task.
            controller.privacy_guard = LoginPrivacyGuard(
                SimpleNamespace(info=privacy_controller_info),
                cancelled=privacy_cancelled,
                game_package=getattr(runtime_controller, "package_name", "com.tencent.tmgp.qjnn"),
            )
            print("Tasker 创建成功。")
            if not tasker.bind(resource, controller) or not tasker.inited:
                raise RuntimeError("MaaFramework Tasker 初始化或绑定失败")
            print("Tasker 绑定 Resource + Controller 成功。")
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            print(f"错误：MaaFramework 初始化失败：{exc}", file=sys.stderr)
            raise TaskRuntimeInitError("runtime", str(exc)) from exc

    def execute(self, task_name: str, *, stop_event: Event | None = None, task_options=None) -> int:
        if self.tasker is None:
            raise RuntimeError("运行环境尚未初始化或已经释放")
        try:
            if not self._privacy_ready(stop_event):
                return 1
        except TaskRunStopped:
            return 130
        return _run_pipeline(self.tasker, task_name, stop_event=stop_event,
                             recovery_runtime=vars(self.resource).get("_back_to_home_runtime")
                             if self.resource is not None else None, task_options=task_options)

    def _privacy_ready(self, stop_event):
        if stop_event is not None:
            self._privacy_stop_signal[0] = stop_event
        def cancelled():
            signal = self._privacy_stop_signal[0]
            return bool(signal is not None and signal.is_set()) or bool(
                getattr(self.tasker, "stopping", False))
        if cancelled():
            raise TaskRunStopped()
        # Wait before submitting a Pipeline so manual login does not consume
        # the existing business/preflight recognition timeout.
        ready = self.controller.post_screencap().wait().succeeded
        if cancelled():
            raise TaskRunStopped()
        return bool(ready)

    def recover_after_failure(self, *, stop_event: Event | None = None) -> bool:
        if stop_event is not None and stop_event.is_set():
            raise TaskRunStopped()
        if self.tasker is None or self.resource is None:
            return False
        if getattr(self.controller, "input_uncertain", False) is True:
            print("[QueueFailureReturnHome] input acknowledgement uncertain; SafeStop, no further input")
            return False
        from qjnn_purchase_record import PurchaseJournal
        try:
            PurchaseJournal.check_resolved_file(DATA_ROOT / "state" / "dreamweaver_purchase_fence.json")
        except (OSError, ValueError, UnicodeError) as exc:
            print(f"[QueueFailureReturnHome] persistent purchase guard unresolved: {exc}; SafeStop")
            return False
        if self._run_state is not None:
            for name in ("dreamweaver_purchase_state.json", "clothes_shop_daily_state.json", "mystery_house_daily_state.json"):
                value = self._run_state.record(name).read()
                if value is not None and (not isinstance(value, dict) or value.get("pending") is not None
                        or value.get("status") == "in_flight" or value.get("paid_in_flight")):
                    print(f"[QueueFailureReturnHome] unresolved payment record={name}; SafeStop")
                    return False
        recovery = vars(self.resource).get("_back_to_home_runtime")
        if recovery is None or not recovery.enabled:
            return False
        if not self._privacy_ready(stop_event):
            return False
        recovery.stop_event = stop_event
        name = "QJNN_QueueFailureReturnHome"
        job = self.tasker.post_task(name, pipeline_override={name: {
            "recognition": "DirectHit", "action": "Custom",
            "custom_action": "qjnn_back_to_home_after_failure",
            "custom_action_param": {"package_name": self.runtime_controller.package_name,
                                    "mail_detail_only": bool(recovery.queue_recovery_blocked)},
            "max_hit": 1, "timeout": 120000,
        }})
        result = _wait_task_result(self.tasker, job, stop_event=stop_event)
        return bool(result and result.status.succeeded and any(
            node.name == name and node.completed for node in result.nodes))

    def close(self):
        try:
            self._close_runtime()
        finally:
            if self._run_state is not None:
                if self._owns_run_state:
                    self._run_state.clear()
                self._run_state = None

    def detach_resume_state(self):
        """Transfer queue progress while releasing all device/native resources."""
        state, self._run_state = self._run_state, None
        return state

    def _close_runtime(self):
        # Binding exposes ownership through __del__, not a public close API.
        # Wait for native callbacks, then drop Tasker before its bound objects.
        # Collect ctypes callback cycles while still on the owning worker thread.
        if self.tasker is not None:
            self.tasker.post_stop().wait()
            self.tasker.set_save_on_error(True)
            self.tasker = None
            gc.collect()
        self.controller = None
        gc.collect()
        self.resource = None
        self.runtime_controller = None
        gc.collect()
        if self._capture_configured:
            from qjnn_common_popup import configure_common_popup_capture
            configure_common_popup_capture(None)
            self._capture_configured = False


def build_task_options_override(task_name, task_options):
    if task_options is None:
        return {}
    if not isinstance(task_options, dict):
        raise ValueError("任务设置必须为对象")
    if task_name == "QJNN_AllianceDaily":
        allow = task_options.get("library_three", False)
        if type(allow) is not bool:
            raise ValueError("整理资料3次设置必须为布尔值")
        return {"QJNN_AllianceDaily": {"custom_action_param": {"library_three": True}}} if allow else {}
    if task_name == "QJNN_Mail":
        allow = task_options.get("claim_friends", False)
        if type(allow) is not bool:
            raise ValueError("领取好友邮件设置必须为布尔值")
        if not allow:
            return {}
        mail_nodes = json.loads((RESOURCE_DIR / 'pipeline/50_mail.json').read_text(encoding='utf-8'))
        return {"QJNN_MailSystemDone": {"next": ["QJNN_FriendMail"]},
                "QJNN_MailSkip": {"next": ["QJNN_FriendMail"]},
                "QJNN_Mail": {"next": ['QJNN_FriendMailResume', *mail_nodes['QJNN_Mail']['next']]},
                "QJNN_MailRewardFlow": {
                    "custom_action_param": {"ready_node": "QJNN_MailEitherTabReady"},
                    "next": ['QJNN_FriendMailResume', *mail_nodes['QJNN_MailRewardFlow']['next']]}}
    if task_name in {"QJNN_Signin", "QJNN_StartAndSignin"}:
        allow = task_options.get("makeup_once", False)
        if type(allow) is not bool:
            raise ValueError("补签设置必须为布尔值")
        params = {"enabled": True, "from_calendar": True}
        if "makeup_tiers" in task_options:
            from signin_options import validate_makeup_tiers
            params["tiers"] = validate_makeup_tiers(task_options["makeup_tiers"])
        limited = task_options.get("limited_event", False)
        if type(limited) is not bool:
            raise ValueError("限时活动签到设置必须为布尔值")
        override = {
            "QJNN_SigninClaimGone": {"next": ["QJNN_MakeupOnce"]},
            "QJNN_MakeupOnce": {"custom_action_param": params},
        } if allow else {}
        if limited and task_name == "QJNN_Signin":
            signin_nodes = json.loads((RESOURCE_DIR / 'pipeline/30_signin.json').read_text(encoding='utf-8'))
            for entry in ('QJNN_Signin', 'QJNN_SigninRewardFlow'):
                override[entry] = {'next': ['QJNN_LimitedSigninResume', *signin_nodes[entry]['next']]}
            override.update({
                "QJNN_SigninCompletedBadgeAbsent": {"next": ["QJNN_LimitedSignin"]},
                "QJNN_SigninSkip": {"next": ["QJNN_LimitedSignin"]},
                "QJNN_LimitedSignin": {"custom_action_param": {"enabled": True}},
                "QJNN_LimitedSigninResume": {"custom_action_param": {"enabled": True}},
            })
            if allow:
                override["QJNN_MakeupOnce"]["next"] = ["QJNN_LimitedSignin"]
        return override
    if task_name == "QJNN_StaminaDaily":
        count = task_options.get("max_items", 4)
        if type(count) is not int or not 1 <= count <= 999:
            raise ValueError("体力任务处理件数必须是 1–999 的整数")
        return {"QJNN_StaminaDaily": {"custom_action_param": {"max_items": count},
                                      "timeout": max(600000, count * 180000)}}
    if task_name == "QJNN_CompetitionDaily":
        votes = task_options.get("votes", 20)
        if type(votes) is not int or not 1 <= votes <= 30:
            raise ValueError("评选赛投票次数必须为 1–30")
        enter = task_options.get("enter", False)
        if type(enter) is not bool:
            raise ValueError("我要参赛设置必须为布尔值")
        params = {"votes": votes}
        if enter:
            params["enter"] = True
        return {"QJNN_CompetitionRun": {"custom_action_param": params,
                                       "timeout": max(600000, votes * 25000 + 90000)}}
    if task_name == "QJNN_MysteryHouseDaily":
        allow = task_options.get("allow_coin_draws", False)
        if type(allow) is not bool:
            raise ValueError("金币抽取设置必须为布尔值")
        params = {"allow_coin_draws": allow}
        if "max_coin_draws" in task_options:
            limit = task_options["max_coin_draws"]
            if type(limit) is not int or not 1 <= limit <= 2:
                raise ValueError("金币抽取上限必须为 1–2 次")
            params["max_coin_draws"] = limit
        return {"QJNN_MysteryHouseDaily": {"custom_action_param": params}}
    raise ValueError(f"此任务不支持专属设置：{task_name}")


def _run_pipeline(tasker, task_name: str, *, stop_event: Event | None = None, recovery_runtime=None,
                  task_options=None) -> int:
    from maa.tasker import Tasker

    task_override = build_task_options_override(task_name, task_options)
    if stop_event is not None and stop_event.is_set():
        return 130

    if task_name == "QJNN_SigninRewardDebug":
        print("[SigninRewardDebug] Starting from current screen")
    if task_name == "QJNN_AllianceCommissionSettlementDebug":
        print("[AllianceCommissionSettlementDebug] Starting from current screen")
    if task_name == "QJNN_AllianceAfterCommissionDebug":
        print("[AllianceAfterCommissionDebug] Starting from current commission map")
    if task_name == "QJNN_CompetitionExitDebug":
        print("[CompetitionExitDebug] Starting from current JudgePage")
    if task_name == "QJNN_CommonPopupDebug":
        print("[CommonPopupDebug] Starting from current screen")
    if task_name == "QJNN_DreamweaverCommissionReadOnlyDebug":
        print("[DreamweaverCommissionReadOnly] navigation + OCR only; no commission actions")

    print(f"正式 Pipeline：{task_name}")
    print("若进入登录页，请手动完成登录；程序不会点击任何登录方式。")

    # Initialize this task's policy before preflight as well: a preflight
    # failure must not inherit a previous task's protected recovery marker.
    runtime_override = (
        build_common_popup_runtime_override()
        if should_run_common_popup_preflight(task_name)
        else {}
    )
    from qjnn_back_to_home import build_runtime_override
    if recovery_runtime is not None:
        settings_path = DATA_ROOT / "config" / "runtime.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
        recovery_runtime.enable_back_to_home_recovery = (
            settings.get("enable_back_to_home_recovery", True)
            and should_run_common_popup_preflight(task_name)
        )
        recovery_runtime.stop_event = stop_event
        runtime_override = build_runtime_override(recovery_runtime, task_name, runtime_override)

    if should_run_common_popup_preflight(task_name):
        print("[CommonPopupPreflight] checking before business recognition")
        try:
            if stop_event is not None and stop_event.is_set():
                raise TaskRunStopped()
            preflight_job = tasker.post_task("QJNN_CommonPopupPreflight")
            preflight_result = _wait_task_result(tasker, preflight_job, stop_event=stop_event)
        except TaskRunStopped:
            Tasker.set_save_on_error(True)
            return 130
        if preflight_result is None:
            print(
                "[CommonPopupPreflight] MaaFramework returned no task detail",
                file=sys.stderr,
            )
            return 1
        print("\n[CommonPopup Preflight Nodes]")
        for node in preflight_result.nodes:
            state = "completed" if node.completed else "incomplete"
            print(f"- {node.name or '<anonymous>'}: {state}")
        preflight_failed = (
            not preflight_result.status.succeeded
            or any(node.name == "QJNN_SafeStop" for node in preflight_result.nodes)
        )
        if preflight_failed:
            print(
                "[CommonPopupPreflight] safe stop; original task was not submitted",
                file=sys.stderr,
            )
            return 1

    for name, options in task_override.items():
        runtime_override.setdefault(name, {}).update(options)
    if runtime_override:
        print(
            "[GlobalTransientGuard] business-first runtime fallback enabled: "
            f"{len(runtime_override)} next lists"
        )

    try:
        if stop_event is not None and stop_event.is_set():
            raise TaskRunStopped()
        from qjnn_back_to_home import run_with_recovery
        result = run_with_recovery(
            tasker, task_name, runtime_override, recovery_runtime,
            lambda job: _wait_task_result(tasker, job, stop_event=stop_event),
        )
    except TaskRunStopped:
        return 130
    finally:
        # Private-login timeout deliberately leaves error screenshots disabled
        # until the task is fully stopped. Restore the runner default only now.
        Tasker.set_save_on_error(True)
        if recovery_runtime is not None:
            print(f"[BackToHomeRecovery] summary: triggers={recovery_runtime.triggers}, "
                  f"back_clicks={recovery_runtime.clicks}, restarts={recovery_runtime.restarts}")
    if result is None:
        print("错误：MaaFramework 未返回任务详情。", file=sys.stderr)
        return 1

    print("\n[Pipeline Nodes]")
    previous_named_node = ""
    for node in result.nodes:
        state = "completed" if node.completed else "incomplete"
        if node.name:
            display_name = node.name
            previous_named_node = node.name
        else:
            predecessor = previous_named_node or "unknown"
            display_name = f"<MaaFramework anonymous bad-next after {predecessor}>"
        print(f"- {display_name}: {state}")

    if any(node.name == "QJNN_HomeStableTimeout" for node in result.nodes):
        print(
            "[Startup] Home base UI detected, but async home controls did not become ready",
            file=sys.stderr,
        )

    failure_nodes = {
        "QJNN_SafeStop",
        "QJNN_SigninFailure",
        "QJNN_MailFailure",
    }
    reached_failures = [node.name for node in result.nodes if node.name in failure_nodes]
    if reached_failures:
        print(
            "任务已安全停止，业务流程未成功："
            + ", ".join(dict.fromkeys(reached_failures)),
            file=sys.stderr,
        )
        return 1

    if not result.status.succeeded:
        print(f"任务失败：{task_name}", file=sys.stderr)
        return 1
    print(f"任务完成：{task_name}")
    return 0



def run(
    task_name: str, *, force_select: bool, device_serial: str | None = None,
    init_only: bool, capture_alliance: bool = False, capture_arena: bool = False,
    capture_competition: bool = False, capture_common_popup: bool = False,
    stop_event: Event | None = None,
) -> int:
    """Compatibility CLI: a single task uses the same batch runtime."""
    if stop_event is not None and stop_event.is_set():
        return 130
    try:
        with TaskRuntime(
            force_select=force_select, device_serial=device_serial,
            capture_alliance=capture_alliance, capture_arena=capture_arena,
            capture_competition=capture_competition,
            capture_common_popup=capture_common_popup, stop_event=stop_event,
        ) as runtime:
            if init_only:
                print("初始化检查完成：未提交 Pipeline，不会执行游戏点击。")
                return 0
            return runtime.execute(task_name, stop_event=stop_event)
    except TaskRunStopped:
        return 130
    except (OSError, RuntimeError, ValueError, ImportError) as exc:
        print(f"错误：任务运行环境失败：{exc}", file=sys.stderr)
        return 1


def main() -> int:
    try:
        available_tasks = load_interface_tasks()
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    args = build_parser(available_tasks).parse_args()
    if args.select_device and args.device_serial:
        print("错误：--select-device 与 --device-serial 不能同时使用", file=sys.stderr)
        return 2
    if args.task not in available_tasks:
        print(f"未知任务：{args.task}", file=sys.stderr)
        print("当前可用任务：", file=sys.stderr)
        for task_id, label in available_tasks.items():
            print(f"  {task_id}: {label}", file=sys.stderr)
        return 2

    return run(
        args.task,
        force_select=args.select_device,
        device_serial=args.device_serial,
        init_only=args.init_only,
        capture_alliance=args.capture_alliance,
        capture_arena=args.capture_arena,
        capture_competition=args.capture_competition,
        capture_common_popup=args.capture_common_popup,
    )


if __name__ == "__main__":
    raise SystemExit(main())

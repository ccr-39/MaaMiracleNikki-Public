from __future__ import annotations

from pathlib import Path

try:
    from maa.agent.agent_server import AgentServer
    from maa.context import Context
    from maa.custom_action import CustomAction
except ImportError:  # MaaFramework is optional for connection-layer tests.
    AgentServer = None

from adb_controller import AdbController, AdbError, AndroidDevice
from device_manager import (
    create_controller,
    discover_mumu_fallback_endpoints,
    has_target_package,
    is_tcp_serial,
    list_devices,
    resolve_adb_path,
    select_device,
)

# Compatibility for the standalone OpenCV entry point.
AdbDevice = AdbController


if AgentServer is not None:
    from qjnn_alliance_commission import (
        AllianceCommissionAction,
        AllianceCommissionMapExitAction,
        AllianceCommissionSettlementDebugAction,
    )
    from qjnn_alliance_daily import AllianceDailyAction, AllianceDailyExitAction
    from qjnn_arena import ArenaDailyAction, ArenaExitDebugAction
    from qjnn_competition import CompetitionDailyAction, CompetitionExitDebugAction
    from qjnn_clothes_shop import ClothesShopPurchaseAction
    from qjnn_decompose import DecomposeDailyAction
    from qjnn_reward import MailRewardAction
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
        FriendSendHeartClickAction,
        FriendSendHeartWaitingResultAction,
    )
    from qjnn_home_navigation import HomeNavigateAction
    from qjnn_home_daily import HomeDailyAction
    from qjnn_home_life_bits import HomeLifeBitsAction
    from qjnn_home_random_visit import HomeRandomVisitAction
    from qjnn_library import LibraryAction
    from qjnn_mystery_house import MysteryHouseDailyAction
    from qjnn_friend_mail import FriendMailAction
    from qjnn_signin_makeup import SigninMakeupAction
    from qjnn_limited_signin import LimitedSigninAction
    from qjnn_world_tour import WorldTourAction
    from qjnn_privacy import PrivateLoginWaitAction
    from qjnn_startup_update import StartupUpdateWaitAction

    AgentServer.custom_action("qjnn_alliance_commission")(AllianceCommissionAction)
    AgentServer.custom_action("qjnn_alliance_commission_settlement_debug")(
        AllianceCommissionSettlementDebugAction
    )
    AgentServer.custom_action("qjnn_alliance_commission_map_exit")(
        AllianceCommissionMapExitAction
    )
    AgentServer.custom_action("qjnn_alliance_daily_exit")(AllianceDailyExitAction)
    AgentServer.custom_action("qjnn_alliance_daily")(AllianceDailyAction)
    AgentServer.custom_action("qjnn_arena_daily")(ArenaDailyAction)
    AgentServer.custom_action("qjnn_arena_exit_debug")(ArenaExitDebugAction)
    AgentServer.custom_action("qjnn_competition_daily")(CompetitionDailyAction)
    AgentServer.custom_action("qjnn_competition_exit_debug")(
        CompetitionExitDebugAction
    )
    AgentServer.custom_action("qjnn_clothes_shop_purchase")(
        ClothesShopPurchaseAction
    )
    AgentServer.custom_action("qjnn_decompose_daily")(DecomposeDailyAction)
    AgentServer.custom_action("qjnn_mail_rewards")(MailRewardAction)
    AgentServer.custom_action("qjnn_daily_task_claim")(DailyTaskClaimAction)
    AgentServer.custom_action("qjnn_stamina_daily")(StaminaDailyAction)
    AgentServer.custom_action("qjnn_dreamweaver_commission")(
        DreamweaverCommissionAction
    )
    AgentServer.custom_action("qjnn_dreamweaver_commission_read_only")(
        DreamweaverCommissionReadOnlyAction
    )
    AgentServer.custom_action("qjnn_common_popup_debug")(CommonPopupDebugAction)
    AgentServer.custom_action("qjnn_common_popup_preflight")(
        CommonPopupPreflightAction
    )
    AgentServer.custom_action("qjnn_common_popup_runtime")(
        CommonPopupRuntimeAction
    )
    AgentServer.custom_action("qjnn_friend_send_heart_click")(
        FriendSendHeartClickAction
    )
    AgentServer.custom_action("qjnn_friend_send_heart_wait_result")(
        FriendSendHeartWaitingResultAction
    )
    AgentServer.custom_action("qjnn_friend_back")(FriendBackAction)
    AgentServer.custom_action("qjnn_home_navigate")(HomeNavigateAction)
    AgentServer.custom_action("qjnn_home_to_p3")(HomeNavigateAction)
    AgentServer.custom_action("qjnn_home_daily")(HomeDailyAction)
    AgentServer.custom_action("qjnn_home_life_bits")(HomeLifeBitsAction)
    AgentServer.custom_action("qjnn_home_random_visit")(HomeRandomVisitAction)
    AgentServer.custom_action("qjnn_library")(LibraryAction)
    AgentServer.custom_action("qjnn_mystery_house_daily")(MysteryHouseDailyAction)
    AgentServer.custom_action("qjnn_friend_mail")(FriendMailAction)
    AgentServer.custom_action("qjnn_signin_makeup_once")(SigninMakeupAction)
    AgentServer.custom_action("qjnn_limited_signin")(LimitedSigninAction)
    AgentServer.custom_action("qjnn_world_tour")(WorldTourAction)
    AgentServer.custom_action("qjnn_private_login_wait")(PrivateLoginWaitAction)
    AgentServer.custom_action("qjnn_startup_update_wait")(StartupUpdateWaitAction)

    @AgentServer.custom_action("qjnn_click_recognized_target")
    class ClickRecognizedTarget(CustomAction):
        """点击自定义识别返回目标框的中心。"""

        def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
            box = argv.box
            if box is None or box.w <= 0 or box.h <= 0:
                return False
            context.tasker.controller.post_click(
                box.x + box.w // 2,
                box.y + box.h // 2,
            ).wait()
            return True

    def _save_failure_frame(controller, prefix: str, label: str) -> bool:
        try:
            import cv2

            controller.post_screencap().wait()
            cached_image = getattr(controller, "cached_image", None)
            image = cached_image() if callable(cached_image) else cached_image
            if image is None or getattr(image, "size", 0) == 0:
                return False
            debug_dir = (
                Path(__file__).resolve().parent.parent
                / "images"
                / "debug"
                / "pipeline"
            )
            debug_dir.mkdir(parents=True, exist_ok=True)
            destination = next(
                (
                    debug_dir / f"{prefix}_{index:03d}.png"
                    for index in range(1, 1000)
                    if not (debug_dir / f"{prefix}_{index:03d}.png").exists()
                ),
                None,
            )
            if destination is None:
                return False
            encoded, data = cv2.imencode(".png", image)
            if not encoded:
                return False
            data.tofile(str(destination))
            print(f"{label}：{destination}")
            return True
        except Exception as exc:
            print(f"保存{label}失败：{exc}")
            return False

    @AgentServer.custom_action("qjnn_save_signin_failure")
    class SaveSigninFailure(CustomAction):
        def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
            return _save_failure_frame(
                context.tasker.controller, "signin_failure", "签到失败调试截图"
            )

    @AgentServer.custom_action("qjnn_save_mail_failure")
    class SaveMailFailure(CustomAction):
        def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
            return _save_failure_frame(
                context.tasker.controller, "mail_failure", "邮箱任务失败调试截图"
            )

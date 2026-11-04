from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class TaskSpec:
    name: str
    entry: str
    order: int
    default_checked: bool
    description: str
    source: str


# 审计依据：interface.json 的正式声明、对应 Pipeline，以及完整日常 CustomAction。
# 不注册 StartAndSignin、联盟委托/图书馆、小屋生活点滴/拜访等组合或内部入口。
_BASE_TASKS = (
    TaskSpec("启动", "QJNN_StartAndEnterHome", 1, True, "启动游戏，登录需手动完成。", "10_startup.json"),
    TaskSpec("签到", "QJNN_Signin", 2, True, "领取签到奖励。", "30_signin.json"),
    TaskSpec("搭配师联盟", "QJNN_AllianceDaily", 4, True, "完成委托和图书馆日常。", "85_alliance_daily.json"),
    TaskSpec("搭配竞技场", "QJNN_ArenaDaily", 5, True, "完成免费竞技场挑战。", "86_arena.json"),
    TaskSpec("搭配评选赛", "QJNN_CompetitionDaily", 6, True, "完成评选赛日常。", "87_competition.json"),
    TaskSpec("织梦人学会", "QJNN_DreamweaverCommission", 7, True, "完成委托和免费探索；缺衣用金币购买。", "84_dreamweaver_commission.json"),
    TaskSpec("小屋", "QJNN_HomeDaily", 8, True, "完成生活点滴和5次拜访。", "96_home_daily.json"),
    TaskSpec("好友", "QJNN_FriendSendHeart", 9, True, "一键送心。", "89_friend_send_heart.json"),
    TaskSpec("体力任务", "QJNN_StaminaDaily", 10, True, "按设置件数刷取服装。", "85_stamina_daily.json"),
    TaskSpec("谜之屋", "QJNN_MysteryHouseDaily", 11, True, "领取免费抽取，可选金币抽取。", "93_mystery_house.json"),
    TaskSpec("世界巡游", "QJNN_WorldTour", 12, True, "前往免费景点游览。", "97_world_tour.json"),
    TaskSpec("服装店", "QJNN_ClothesShopPurchase", 13, False, "金币购买3件“搭配购物节”。", "94_clothes_shop.json"),
    TaskSpec("分解重构", "QJNN_DecomposeDaily", 14, False, "分解“无用途”服装，领取奖励。", "95_decompose.json"),
)


# GUI grouping only: execution continues to use the original pipeline IDs.
REWARDS_ENTRY = "QJNN_RewardClaim"
REWARD_TASKS = (
    TaskSpec("邮箱", "QJNN_Mail", 15, False, "领取系统邮件，可选好友邮件。", "50_mail.json"),
    TaskSpec("每日任务", "QJNN_DailyTaskClaim", 16, False, "领取每日任务奖励。", "98_daily_task_claim.json"),
)
REWARD_ENTRIES = frozenset(task.entry for task in REWARD_TASKS)
REWARDS_TASK = TaskSpec("奖励领取", REWARDS_ENTRY, 15, False,
                        "领取所选奖励。", "")
TASKS = _BASE_TASKS + (REWARDS_TASK,)
EXECUTION_TASKS = _BASE_TASKS + REWARD_TASKS


def visible_task_entries(entries):
    return {REWARDS_ENTRY if entry in REWARD_ENTRIES else entry for entry in entries}


def build_queue(selected_entries) -> tuple[TaskSpec, ...]:
    selected = set(selected_entries)
    unknown = selected - {task.entry for task in EXECUTION_TASKS}
    if unknown:
        raise ValueError("未知任务入口：" + ", ".join(sorted(unknown)))
    return tuple(task for task in sorted(EXECUTION_TASKS, key=lambda task: task.order) if task.entry in selected)


def validate_registry() -> None:
    """只读审计，既不导入 ADB 实例，也不初始化 MaaFramework。"""
    from tools.run_task import load_interface_tasks

    declared = load_interface_tasks()
    if len({task.entry for task in EXECUTION_TASKS}) != len(EXECUTION_TASKS) or len({task.order for task in EXECUTION_TASKS}) != len(EXECUTION_TASKS):
        raise ValueError("任务注册表入口或顺序重复")
    for task in EXECUTION_TASKS:
        if task.entry not in declared:
            raise ValueError(f"正式 interface 未声明入口：{task.entry}")
        source = ROOT / "assets" / "resource" / "pipeline" / task.source
        nodes = json.loads(source.read_text(encoding="utf-8"))
        if task.entry not in nodes:
            raise ValueError(f"Pipeline 缺少入口：{task.entry}（{task.source}）")

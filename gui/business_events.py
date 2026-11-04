"""已审计旧日志的兼容适配，不推断游戏状态，不改变任务返回值。

只接受指定正式任务下的完整、已知输出格式/节点事件；未知日志不成为用户事件。
以后业务主动发事件时可逐项替换此兼容层。
"""
import re
from threading import RLock

from gui.user_events import EventType

# key=(正式任务入口, 框架消息, 既有节点名)，value=(事件类型, 中文说明)。
# 审计来源：pipeline/30_signin、50_mail、86_arena、87_competition、
# 89_friend_send_heart、94_clothes_shop、95_decompose、96_home_daily。
NODE_RESULTS = {
    ("QJNN_StartAndEnterHome", "Node.Action.Starting", "QJNN_UpdateConfirm"): (EventType.TASK_PROGRESS, "检测到游戏版本更新，正在确认更新"),
    ("QJNN_StartAndEnterHome", "Node.Action.Starting", "QJNN_UpdateWait"): (EventType.TASK_PROGRESS, "正在等待游戏更新完成，最多 10 分钟"),
    ("QJNN_StartAndEnterHome", "Node.Action.Succeeded", "QJNN_UpdateWait"): (EventType.TASK_PROGRESS, "更新等待结束，继续启动流程"),
    ("QJNN_Signin", "Node.Recognition.Succeeded", "QJNN_SigninSkip"): (EventType.TASK_SKIPPED, "跳过签到：今日没有可领取的免费签到奖励"),
    ("QJNN_Signin", "Node.Recognition.Succeeded", "QJNN_SigninCompletedBadgeAbsent"): (EventType.TASK_PROGRESS, "今日签到已完成"),
    ("QJNN_Signin", "Node.Recognition.Succeeded", "QJNN_SigninCompletedOnHome"): (EventType.TASK_PROGRESS, "已确认返回主页"),
    ("QJNN_Mail", "Node.Recognition.Succeeded", "QJNN_MailSkip"): (EventType.TASK_SKIPPED, "跳过邮箱：没有待领取的系统邮件通知"),
    ("QJNN_Mail", "Node.Recognition.Succeeded", "QJNN_MailNoClaimable"): (EventType.TASK_PROGRESS, "系统邮件没有可领取的奖励"),
    ("QJNN_Mail", "Node.Recognition.Succeeded", "QJNN_HomeAfterMail"): (EventType.TASK_PROGRESS, "系统邮件处理完成，已确认返回主页"),
    ("QJNN_FriendSendHeart", "Node.Recognition.Succeeded", "QJNN_FriendSuccessResult"): (EventType.TASK_PROGRESS, "好友送心成功"),
    ("QJNN_FriendSendHeart", "Node.Recognition.Succeeded", "QJNN_FriendNoTargetResult"): (EventType.TASK_SKIPPED, "没有可送心的好友，本次送心结束"),
    ("QJNN_FriendSendHeart", "Node.Recognition.Succeeded", "QJNN_HomeAfterFriendSendHeart"): (EventType.TASK_PROGRESS, "已确认返回主页"),
    ("QJNN_ArenaDaily", "Node.Action.Succeeded", "QJNN_ArenaRun"): (EventType.TASK_PROGRESS, "竞技场阶段完成，已确认返回主页"),
    ("QJNN_CompetitionDaily", "Node.Action.Succeeded", "QJNN_CompetitionRun"): (EventType.TASK_PROGRESS, "评选赛阶段完成，已确认返回主页"),
    ("QJNN_ClothesShopPurchase", "Node.Action.Succeeded", "QJNN_ClothesShopBusinessState"): (EventType.TASK_PROGRESS, "服装店流程已结束，已确认返回主页"),
    ("QJNN_DecomposeDaily", "Node.Action.Succeeded", "QJNN_DecomposeDaily"): (EventType.TASK_PROGRESS, "分解任务已结束，已确认返回主页"),
}

# 完整原文的精确映射；子任务结果只报告阶段，不宣称整个正式任务完成。
TEXT_RESULTS = {
    "QJNN_DecomposeDaily": {
        "[Decompose] no unused clothes; returning home": "没有无用途服装，返回主页",
    },
    "QJNN_HomeDaily": {
        "[HomeDaily] life bits already running; no restart needed": "生活点滴已有任务倒计时，本次无需重新派遣，继续随便逛逛",
        "[HomeDaily] life bits completed; 8-hour task running": "生活点滴已处理，8 小时任务运行中，继续随便逛逛",
        "[HomeDaily] completed; HOME P2 confirmed": "小屋日常已完成，已确认返回主页",
    },
    "QJNN_AllianceDaily": {
        "[AllianceDaily] commission locked by alliance level; continue library": "联盟等级不足，跳过委托，继续图书馆日常",
        "已返回搭配师联盟首页": "已返回联盟首页",
        "已退出搭配师联盟并确认主页 P3": "已退出联盟并确认返回主页",
        "未检测到免费完成：不点击整理资料": "图书馆暂无免费整理机会",
        "已识别恭喜获得": "已确认图书馆奖励页面",
    },
    "QJNN_DreamweaverCommission": {
        "[DreamweaverCommission] shopping blocked: price/currency unconfirmed": "未能确认金币价格，停止购买",
        "[DreamweaverCommission] shopping blocked: quantity unconfirmed": "购买数量未确认，停止购买",
        "[DreamweaverCommission] shopping blocked: modal product unconfirmed": "弹窗商品与目标不符，停止购买",
        "[DreamweaverCommission] shopping inventory sufficient; returning to commission": "服装数量足够，返回提交委托",
        "[DreamweaverCommission] shopping stopped: purchase record unavailable or unresolved": "本次运行的购买记录异常或交易未确认，已停止购物。请核对游戏内的购买结果",
        "[DreamweaverCommission] shopping result unconfirmed; retry blocked": "购买结果未确认，已阻止再次购买。请核对游戏内的金币和库存",
        "[DreamweaverCommission] float island free exploration already used": "浮梦岛今日免费探索已完成",
        "[DreamweaverCommission] returned from float island": "浮梦岛阶段结束，已返回学会",
        "[DreamweaverCommission] HOME P3 confirmed": "已确认返回主页",
    },
    "QJNN_WorldTour": {
        "[WorldTour] previous city ended; confirm once, then select a new city": "上段旅行已结束，正在确认并重新选择城市",
        "[WorldTour] HOME P1 confirmed; completed": "世界巡游阶段完成，已确认返回主页",
    },
    "QJNN_MysteryHouseDaily": {
        "[MysteryHouse] completed": "谜之屋每日阶段已完成",
        "[MysteryHouse] free-only completed; coin draws disabled": "免费抽取已完成，已跳过金币抽取",
    },
    "QJNN_ClothesShopPurchase": {
        "[ClothesShop] already purchased in this queue; skipping repeat purchase": "本次运行已购买，跳过重复购买",
        "[ClothesShop] purchase blocked: price/currency unconfirmed": "未能确认金币价格，停止购买",
    },
    "QJNN_StaminaDaily": {
        "[StaminaDaily] all categories exhausted; returned HOME": "发型到妆容已处理完现有服装，已返回主页",
    },
}

# 审计来源：agent/qjnn_arena.py:769/808、qjnn_alliance_commission.py:464、
# qjnn_competition.py:1138/1235、qjnn_mystery_house.py:1395/1428。
COUNT_RESULTS = (
    ("QJNN_StaminaDaily", r"\[StaminaDaily\] items processed: (\d{1,3})/(\d{1,3})", "体力任务已处理：{0}/{1} 件"),
    ("QJNN_StaminaDaily", r"\[StaminaDaily\] items completed: (\d{1,3})/(\d{1,3})", "体力任务处理结束：{0}/{1} 件，已返回主页"),
    ("QJNN_DreamweaverCommission", r"\[DreamweaverCommission\] shopping verified: (\d{1,6}) coins; quantity=([12])", "学会购物已确认：{0} 金币，共 {1} 件"),
    ("QJNN_ArenaDaily", r"\[Arena\] remaining: ([0-5])/5", "今日剩余挑战次数：{0}/5"),
    ("QJNN_AllianceDaily", r"今日剩余次数：([0-5])/5", "今日剩余委托次数：{0}/5"),
    ("QJNN_CompetitionDaily", r"\[Competition\] remaining: (\d|[12]\d|30)/30", "今日剩余评选次数：{0}/30"),
    ("QJNN_CompetitionDaily", r"\[Competition\] successful votes: (\d{1,2})/(\d{1,2})", "本次已完成评选投票：{0}/{1}"),
    ("QJNN_CompetitionDaily", r"\[Competition\] remaining: 0/30; ending normally at (\d{1,2})/(\d{1,2})", "今日评选次数已用完，本次已投票 {0}/{1}"),
    ("QJNN_MysteryHouseDaily", r"\[MysteryHouse\] free draw completed: ([12])/2", "谜之阁免费抽取已完成：{0}/2"),
    ("QJNN_MysteryHouseDaily", r"\[MysteryHouse\] paid progress: ([0-2])/2", "谜之阁金币抽取进度：{0}/2"),
    ("QJNN_MysteryHouseDaily", r"\[MysteryHouse\] coin budget completed: ([0-2])/([12])", "金币抽取已结束：当日已完成 {0} 次，当前上限 {1} 次"),
    ("QJNN_ClothesShopPurchase", r"\[ClothesShop\] purchase verified: (\d{1,6}) coins; quantity=3", "购买已确认：{0} 金币，共 3 件"),
)


class BusinessEvents:
    def __init__(self, events):
        self.events = events
        self._task = None
        self._seen = set()
        self._lock = RLock()

    def set_task(self, task):
        with self._lock:
            self._task = task
            self._seen.clear()

    def _publish(self, kind, text):
        if (kind, text) not in self._seen:
            self._seen.add((kind, text))
            self.events.publish(kind, text, "INFO", self._task.name, task_entry=self._task.entry)

    def text(self, level, line):
        with self._lock:
            if self._task is None:
                return
            entry = self._task.entry
            text = TEXT_RESULTS.get(entry, {}).get(line)
            if text:
                self._publish(EventType.TASK_PROGRESS, text)
            for source, pattern, template in COUNT_RESULTS:
                if source == entry:
                    match = re.fullmatch(pattern, line)
                    if match:
                        self._publish(EventType.TASK_PROGRESS, template.format(*match.groups()))

    def framework(self, message, details):
        with self._lock:
            if self._task is None:
                return
            name = details.get("name")
            if not isinstance(name, str):
                return
            result = NODE_RESULTS.get((self._task.entry, message, name))
            if result:
                self._publish(*result)

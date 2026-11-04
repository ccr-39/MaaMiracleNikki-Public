from __future__ import annotations

from app_paths import GUI_CONFIG_PATH, legacy_gui_candidates

import json
import logging
import os
import re
from ipaddress import ip_address
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from gui.task_registry import EXECUTION_TASKS
from signin_options import validate_makeup_tiers

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
CONNECTION_PROFILES = ("General", "MuMu")


def validate_connection_fields(config, *, require_address=True):
    """Validate form syntax without searching paths or contacting a device."""
    address = config.adb_address.strip()
    if not address:
        if require_address and not config.dry_run:
            raise ValueError("请填写连接地址，或先检测并选择设备")
    elif any(char.isspace() for char in address):
        raise ValueError("连接地址不能包含空格或换行")
    elif ":" in address:
        match = re.fullmatch(r"(?:\[[0-9A-Fa-f:]+\]|[A-Za-z0-9_.-]+):(\d+)", address)
        if match is None or not 1 <= int(match.group(1)) <= 65535:
            raise ValueError("连接地址应为 IP:端口，端口范围为 1–65535")
        host = address.rsplit(":", 1)[0].strip("[]")
        if address.startswith("[") or re.fullmatch(r"\d+(?:\.\d+){3}", host):
            try:
                ip_address(host)
            except ValueError as exc:
                raise ValueError("IP 地址格式无效，请检查连接地址") from exc
    elif re.fullmatch(r"\d+(?:\.\d+){3}", address):
        raise ValueError("IP 地址后需要填写端口，例如 127.0.0.1:16416")
    elif not re.fullmatch(r"[A-Za-z0-9_.-]+", address):
        raise ValueError("请填写 IP:端口或有效的设备序列号")
    if any(char in config.adb_path for char in "\r\n\x00"):
        raise ValueError("ADB 路径不能包含换行或无效字符")


@dataclass
class GuiConfig:
    adb_address: str = ""
    auto_connect: bool = False
    remember_selection: bool = True
    log_level: str = "INFO"
    selected_entries: list[str] = field(default_factory=list)
    dry_run: bool = False
    adb_path: str = ""
    connection_profile: str = "General"
    continue_on_failure: bool = False
    competition_votes: int = 20
    competition_enter: bool = False
    reward_enabled: bool = False
    reward_mail: bool = False
    reward_daily: bool = False
    mail_claim_friends: bool = False
    alliance_library_three: bool = False
    signin_makeup_once: bool = False
    signin_limited_event: bool = False
    signin_makeup_tiers: list[int] = field(default_factory=lambda: [50])
    mystery_allow_coin_draws: bool = False
    mystery_max_coin_draws: int = 2
    stamina_max_items: int = 4
    schedule_enabled: bool = False
    schedule_times: str = "08:00"
    close_emulator_after_completion: bool = False
    emulator_auto_start: bool = False
    emulator_path: str = ""
    emulator_arguments: str = ""
    auto_check_updates: bool = True

    def validate_task_options(self):
        for key in ("reward_enabled", "reward_mail", "reward_daily"):
            if type(getattr(self, key)) is not bool:
                raise ValueError(f"配置 {key} 必须为布尔值")
        if type(self.competition_enter) is not bool:
            raise ValueError("我要参赛设置必须为布尔值")
        if type(self.alliance_library_three) is not bool:
            raise ValueError("整理资料3次设置必须为布尔值")
        if type(self.mail_claim_friends) is not bool:
            raise ValueError("领取好友邮件设置必须为布尔值")
        if type(self.signin_makeup_once) is not bool:
            raise ValueError("补签设置必须为布尔值")
        if type(self.signin_limited_event) is not bool:
            raise ValueError("限时活动签到设置必须为布尔值")
        validate_makeup_tiers(self.signin_makeup_tiers)
        from gui.scheduler import parse_times
        for key in ("schedule_enabled", "emulator_auto_start", "close_emulator_after_completion", "auto_check_updates"):
            if type(getattr(self, key)) is not bool:
                raise ValueError(f"配置 {key} 必须为布尔值")
        parse_times(self.schedule_times)
        for key in ("emulator_path", "emulator_arguments"):
            value = getattr(self, key)
            if not isinstance(value, str) or any(char in value for char in "\r\n\x00"):
                raise ValueError("模拟器路径和参数不能包含换行或无效字符")
        if self.emulator_auto_start and not self.emulator_path.strip():
            raise ValueError("开启自动启动模拟器前，请先选择模拟器 exe 文件")
        if type(self.stamina_max_items) is not int or not 1 <= self.stamina_max_items <= 999:
            raise ValueError("体力任务处理件数必须是 1–999 的整数")
        if type(self.competition_votes) is not int or not 1 <= self.competition_votes <= 30:
            raise ValueError("评选赛投票次数必须是 1–30 的整数")
        if type(self.mystery_allow_coin_draws) is not bool:
            raise ValueError("谜之屋金币抽取开关必须为布尔值")
        if type(self.mystery_max_coin_draws) is not int or not 1 <= self.mystery_max_coin_draws <= 2:
            raise ValueError("谜之屋金币抽取上限必须为 1–2 次")


class ConfigStore:
    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else GUI_CONFIG_PATH
        self._migrate_legacy = path is None

    def load(self) -> GuiConfig:
        if self._migrate_legacy:
            from gui.config_migration import migrate_legacy_gui_config
            try:
                migrate_legacy_gui_config(self.path, legacy_gui_candidates())
            except (OSError, ValueError):
                # A backup/report failure must not hide a valid current profile.
                logging.getLogger(__name__).exception("Legacy GUI migration failed; original files retained")
        if not self.path.exists():
            return GuiConfig()
        payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("GUI 配置必须是 JSON 对象")
        result = GuiConfig()
        for key in ("reward_enabled", "reward_mail", "reward_daily", "auto_connect", "remember_selection", "dry_run", "continue_on_failure", "mystery_allow_coin_draws",
                    "schedule_enabled", "emulator_auto_start", "close_emulator_after_completion", "auto_check_updates", "signin_makeup_once", "signin_limited_event", "mail_claim_friends", "alliance_library_three", "competition_enter"):
            if key in payload:
                if type(payload[key]) is not bool:
                    raise ValueError(f"配置 {key} 必须为布尔值")
                setattr(result, key, payload[key])
        for key in ("competition_votes", "mystery_max_coin_draws", "stamina_max_items", "signin_makeup_tiers"):
            if key in payload:
                setattr(result, key, payload[key])
        for key in ("schedule_times", "emulator_path", "emulator_arguments"):
            if key in payload:
                setattr(result, key, payload[key])
        result.validate_task_options()
        if "adb_address" in payload:
            if not isinstance(payload["adb_address"], str):
                raise ValueError("ADB 地址必须为字符串")
            result.adb_address = payload["adb_address"].strip()
        if "adb_path" in payload:
            if not isinstance(payload["adb_path"], str):
                raise ValueError("ADB 路径必须为字符串")
            result.adb_path = payload["adb_path"].strip()
        if "connection_profile" in payload:
            if payload["connection_profile"] not in CONNECTION_PROFILES:
                raise ValueError("连接配置无效")
            # MuMu previously had a separate GUI option, with identical ADB
            # commands. Migrate that saved option to the unified profile.
            result.connection_profile = "General"
        if "log_level" in payload:
            if payload["log_level"] not in LOG_LEVELS:
                raise ValueError("日志等级无效")
            result.log_level = payload["log_level"]
        entries = payload.get("selected_entries", result.selected_entries)
        if not isinstance(entries, list) or any(not isinstance(entry, str) for entry in entries):
            raise ValueError("勾选状态必须为入口字符串列表")
        if not result.remember_selection:
            result.reward_enabled = result.reward_mail = result.reward_daily = False
        if result.remember_selection:
            # 旧配置中已删除的任务忽略；空数组表示用户明确清空。
            result.reward_mail = payload.get("reward_mail", "QJNN_Mail" in entries)
            result.reward_daily = payload.get("reward_daily", "QJNN_DailyTaskClaim" in entries)
            result.reward_enabled = payload.get("reward_enabled", result.reward_mail or result.reward_daily)
            selected = set(entries) - {"QJNN_Mail", "QJNN_DailyTaskClaim"}
            if result.reward_enabled:
                if result.reward_mail:
                    selected.add("QJNN_Mail")
                if result.reward_daily:
                    selected.add("QJNN_DailyTaskClaim")
            result.selected_entries = [task.entry for task in EXECUTION_TASKS if task.entry in selected]
        return result

    def save(self, config: GuiConfig) -> None:
        config.validate_task_options()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(config)
        if payload["connection_profile"] == "MuMu":
            payload["connection_profile"] = "General"
        if not config.remember_selection:
            payload.pop("selected_entries")
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, suffix=".tmp", delete=False) as stream:
                temp_path = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

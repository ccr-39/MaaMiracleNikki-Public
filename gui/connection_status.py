"""Connection presentation uses confirmed stages, never guesses from log text."""
from dataclasses import dataclass
from enum import Enum


class TestStage(str, Enum):
    UNTESTED = "untested"
    DETECTING = "detecting"
    DETECTED = "detected"
    TESTING = "testing"
    PASSED = "passed"
    FAILED = "failed"
    INVALID = "invalid"


@dataclass(frozen=True)
class ConnectionTestReport:
    stage: TestStage = TestStage.UNTESTED
    time: str = ""
    details: str = ""

    @property
    def text(self):
        return {
            TestStage.UNTESTED: "尚未测试",
            TestStage.DETECTING: "正在检测设备…",
            TestStage.DETECTED: "已检测设备，尚未测试截图",
            TestStage.TESTING: "正在测试连接与截图…",
            TestStage.PASSED: f"上次通过 · {self.time}",
            TestStage.FAILED: f"上次失败 · {self.time}",
            TestStage.INVALID: "设置无效，未执行测试",
        }[self.stage]

    @property
    def tone(self):
        if self.stage is TestStage.PASSED:
            return "success"
        if self.stage in {TestStage.FAILED, TestStage.INVALID}:
            return "error"
        return "muted"


class SessionStage(str, Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FAILED = "failed"
    ENDED = "ended"
    END_ERROR = "end_error"
    OFFLINE = "offline"
    OFFLINE_ENDED = "offline_ended"


@dataclass
class RuntimeConnectionStatus:
    stage: SessionStage = SessionStage.IDLE
    actual_address: str = ""
    connected_at: str = ""
    failure: str = ""

    @property
    def text(self):
        return {
            SessionStage.IDLE: "尚未开始",
            SessionStage.CONNECTING: "正在检查连接…",
            SessionStage.CONNECTED: "已连接 · 本次运行",
            SessionStage.FAILED: self.failure or "连接 / 初始化未完成",
            SessionStage.ENDED: "本次运行已结束",
            SessionStage.END_ERROR: "运行异常结束，请查看日志",
            SessionStage.OFFLINE: "离线演练，不连接设备",
            SessionStage.OFFLINE_ENDED: "离线演练已结束，未连接设备",
        }[self.stage]

    @property
    def tone(self):
        if self.stage is SessionStage.CONNECTED:
            return "success"
        if self.stage in {SessionStage.FAILED, SessionStage.END_ERROR}:
            return "error"
        return "muted"

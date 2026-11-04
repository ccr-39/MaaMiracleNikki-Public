"""User guidance based on known execution stages, never raw log keywords."""
from dataclasses import dataclass


@dataclass(frozen=True)
class FailureInfo:
    kind: str
    title: str
    advice: str


class ExecutionIssue(RuntimeError):
    def __init__(self, info):
        super().__init__(info.title)
        self.info = info


CONNECTION = FailureInfo("connection", "设备连接失败",
                         "请检查模拟器是否启动、ADB 调试是否开启，确认地址后点击“重新连接”。")
INITIALIZATION = FailureInfo("initialization", "运行环境初始化失败",
                             "请检查依赖和识别资源是否完整，具体错误见详细日志。")
RECOVERY = FailureInfo("recovery", "无法确认返回游戏主页",
                       "请手动回到游戏主页，再点击“继续未完成”。")
CLEANUP = FailureInfo("cleanup", "运行环境释放失败", "请重启助手后再运行。")


def describe_failure(exc, *, stage, task_name=""):
    if isinstance(exc, ExecutionIssue):
        return exc.info
    if stage == "prepare":
        return FailureInfo("prepare", "设备连接或运行环境初始化失败",
                           "请先在连接设置中测试截图。截图正常时，查看详细日志中的错误。")
    if stage == "recovery":
        return RECOVERY
    if stage == "cleanup":
        return CLEANUP
    return FailureInfo("task", f"{task_name or '任务'}未能完成",
                       "请登录游戏、关闭弹窗并回到主页，再重试。已完成的任务不会重跑。")

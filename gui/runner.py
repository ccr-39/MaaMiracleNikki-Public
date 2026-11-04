from __future__ import annotations

import logging
import weakref
from time import monotonic
from threading import Event
from typing import Protocol
from dataclasses import replace

from PySide6.QtCore import QThread, Signal

from gui.config import GuiConfig
from gui.task_registry import TaskSpec, build_queue, validate_registry, visible_task_entries
from gui.log_bridge import DetailedLog, attach_framework_logs, detach_framework_logs, forward_output
from gui.user_events import EventType, UserEvents
from gui.business_events import BusinessEvents
from gui.failures import CONNECTION, INITIALIZATION, ExecutionIssue, describe_failure
from gui.branding import GUI_LOGGER_NAME

logger = logging.getLogger(GUI_LOGGER_NAME)


class ExecutionStopped(Exception):
    pass


class QueueRecoveryFailed(RuntimeError):
    pass


class TaskExecutor(Protocol):
    def prepare(self, stop: Event) -> str | None: ...
    def execute(self, task: TaskSpec, stop: Event) -> None: ...
    def close(self) -> None: ...


class OfflineExecutor:
    """仅演练正式入口提交顺序；绝不初始化 ADB / Resource / Tasker。"""

    is_real = False

    def __init__(self, duration: float = 0.6):
        self.duration = duration

    def prepare(self, stop: Event) -> None:
        if stop.is_set():
            raise ExecutionStopped()

    def close(self) -> None:
        pass

    def execute(self, task: TaskSpec, stop: Event) -> None:
        logger.info("离线演练入口：%s", task.entry)
        if stop.wait(self.duration):
            raise ExecutionStopped()


class MaaTaskExecutor:
    """整批复用 tools.run_task 的运行会话，不在 GUI 初始化 Maa/ADB。"""

    is_real = True
    supports_resume_state = True

    def __init__(self, config: GuiConfig):
        config.validate_task_options()
        self.device_serial = config.adb_address.strip()
        self.auto_connect = config.auto_connect
        self.adb_path = config.adb_path.strip() or None
        self.connection_profile = config.connection_profile
        self.task_options = {
            "QJNN_Mail": {"claim_friends": config.mail_claim_friends},
            "QJNN_Signin": {"makeup_once": config.signin_makeup_once,
                            "makeup_tiers": list(config.signin_makeup_tiers)},
            "QJNN_CompetitionDaily": {"votes": config.competition_votes},
            "QJNN_StaminaDaily": {"max_items": config.stamina_max_items},
            "QJNN_MysteryHouseDaily": {"allow_coin_draws": config.mystery_allow_coin_draws,
                                      "max_coin_draws": config.mystery_max_coin_draws},
        }
        if config.alliance_library_three:
            self.task_options["QJNN_AllianceDaily"] = {"library_three": True}
        if config.signin_limited_event:
            self.task_options["QJNN_Signin"]["limited_event"] = True
        if config.competition_enter:
            self.task_options["QJNN_CompetitionDaily"]["enter"] = True
        self.runtime = None
        self.resume_state = None
        self.startup_config = replace(config)
        self.log_callback = None
        self.framework_callback = None
        self.startup_callback = None
        self._sink_ids = ()
        if not self.device_serial:
            raise ValueError("设备地址不能为空")

    def prepare(self, stop: Event) -> str:
        from tools.run_task import TaskRuntime, TaskRunStopped, TaskRuntimeInitError

        if stop.is_set():
            raise ExecutionStopped()
        if self.startup_config.emulator_auto_start:
            from gui.emulator import ensure_emulator
            ensure_emulator(self.startup_config, stop, log=self.startup_callback or print)
        if stop.is_set():
            raise ExecutionStopped()
        self.runtime = TaskRuntime(
            device_serial=self.device_serial, auto_connect=self.auto_connect,
            adb_path=self.adb_path, profile=self.connection_profile,
            stop_event=stop,
            **({'run_state': self.resume_state} if self.resume_state is not None else {}),
        )
        try:
            self.runtime.__enter__()
        except TaskRunStopped:
            raise ExecutionStopped() from None
        except TaskRuntimeInitError as exc:
            raise ExecutionIssue(CONNECTION if exc.phase == "connection" else INITIALIZATION) from exc
        if self.log_callback is not None:
            self._sink_ids = attach_framework_logs(self.runtime.tasker, self.log_callback, self.framework_callback)
        self.device_serial = self.runtime.runtime_controller.serial
        return self.device_serial

    def close(self) -> None:
        if self.runtime is not None:
            if self._sink_ids:
                detach_framework_logs(self.runtime.tasker, self._sink_ids, self.log_callback)
                self._sink_ids = ()
            detach = getattr(self.runtime, 'detach_resume_state', None)
            if callable(detach):
                state = detach()
                if state is not None:
                    self.resume_state = state
            self.runtime.close()
            self.runtime = None

    def execute(self, task: TaskSpec, stop: Event) -> None:
        if stop.is_set():
            raise ExecutionStopped()
        if self.runtime is None:
            raise RuntimeError("运行环境尚未初始化")
        logger.info("实机执行入口：%s", task.entry)
        options = {"task_options": dict(self.task_options[task.entry])} if task.entry in self.task_options else {}
        result = self.runtime.execute(task.entry, stop_event=stop, **options)
        if result == 130 or stop.is_set():
            raise ExecutionStopped()
        if result:
            raise RuntimeError(f"现有 run_task 返回错误码 {result}；详情见 runner 日志")

    def recover_after_failure(self, stop: Event) -> bool:
        from tools.run_task import TaskRunStopped
        if self.runtime is None:
            return False
        try:
            return self.runtime.recover_after_failure(stop_event=stop)
        except TaskRunStopped:
            raise ExecutionStopped() from None


class QueueWorker(QThread):
    task_started = Signal(str)
    state_changed = Signal(str)
    device_changed = Signal(str, str)
    outcome = Signal(str, str)
    user_event = Signal(object)
    problem_reported = Signal(object)
    # 兼容现有离线测试；只镜像用户事件，不再接收原始日志。
    log_message = Signal(str, str, str)

    def __init__(self, selected_entries, executor: TaskExecutor, parent=None, *, detail_log_path=None,
                 continue_on_failure=False):
        super().__init__(parent)
        self.queue = build_queue(selected_entries)
        self.executor = executor
        self.continue_on_failure = continue_on_failure
        self.stop_event = Event()
        self.detailed_log_path = detail_log_path
        self._details = None
        worker_ref = weakref.ref(self)
        def emit_event(event):
            worker = worker_ref()
            if worker is not None:
                if worker._details is not None:
                    worker._details.record(event.kind.value, event.text)
                worker.user_event.emit(event)
                worker.log_message.emit(event.time, event.level, event.text)
        self._events = UserEvents(emit_event)
        self._business = BusinessEvents(self._events)
        if getattr(executor, "is_real", False):
            executor.log_callback = self._framework_detail
            executor.framework_callback = self._business.framework
            executor.startup_callback = self._startup_progress

    def _startup_progress(self, text):
        message = text.removeprefix("[Emulator] ")
        self._events.publish(EventType.TASK_PROGRESS, message)
        self.state_changed.emit(message)

    def _framework_detail(self, level, text):
        if self._details is not None:
            self._details.record(level, text)

    def request_stop(self) -> None:
        with self._events.lock:
            if not self.stop_event.is_set():
                self.stop_event.set()
                self._events.publish(EventType.STOP_REQUESTED, "用户请求安全停止", "WARNING")

    def run(self) -> None:
        queue_start = monotonic()
        with forward_output(self._business.text, self.detailed_log_path) as details:
            self._details = details
            self.detailed_log_path = details.path
            state, message = self._run_queue()
            config = getattr(self.executor, "startup_config", None)
            if (state == "completed" and self.queue and not self.stop_event.is_set()
                    and getattr(self.executor, "is_real", False)
                    and getattr(config, "close_emulator_after_completion", False)):
                from gui.emulator import close_emulator
                close_emulator(config, self.executor.device_serial, self.stop_event,
                               log=lambda text, level: self._events.publish(EventType.TASK_PROGRESS, text, level))
            if state == "completed":
                self._events.publish(EventType.QUEUE_COMPLETED, "整个队列已完成", "SUCCESS")
            elif state == "partial":
                self._events.publish(EventType.QUEUE_COMPLETED, message, "WARNING")
            elif state == "stopped":
                self._events.publish(EventType.STOP_COMPLETED, "队列已停止", "WARNING")
        self._details = None
        if details.error is not None:
            self._events.publish(EventType.TASK_PROGRESS, "详细日志未能完整保存，请检查日志目录和磁盘空间。", "WARNING")
        if getattr(self.executor, "is_real", False):
            self.executor.log_callback = None
            self.executor.framework_callback = None
            self.executor.startup_callback = None
        elapsed = max(0.0, monotonic() - queue_start)
        seconds = int(elapsed)
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        duration = (f"{hours} 小时 {minutes} 分 {seconds} 秒" if hours else
                    f"{minutes} 分 {seconds} 秒" if minutes else f"{elapsed:.1f} 秒")
        # Append after output buffers and cleanup logs have been flushed, so
        # timing is the last line for success, failure and user cancellation.
        timing_log = DetailedLog(self.detailed_log_path)
        self._details = timing_log
        try:
            self._events.publish(EventType.QUEUE_FINISHED, f"本次流程总用时：{duration}",
                                 elapsed_seconds=elapsed)
        finally:
            timing_log.close()
            self._details = None
        self.outcome.emit(state, message)

    def _run_queue(self):
        current = "任务队列"
        completed = set()
        failed = set()
        entries = {task.entry for task in self.queue}
        groups = visible_task_entries(entries)

        def counts():
            succeeded = groups - visible_task_entries(entries - completed)
            failures = visible_task_entries(failed)
            return len(succeeded), len(failures), len(groups - succeeded - failures)
        task_start = None
        current_entry = None
        state, message = "completed", "已完成"
        mode = "实机执行" if getattr(self.executor, "is_real", False) else "离线演练"
        self._events.publish(EventType.QUEUE_STARTED, f"队列开始（{mode}，共 {len(groups)} 项）")
        try:
            validate_registry()
            if self.stop_event.is_set():
                raise ExecutionStopped()
            current = "连接 / 初始化"
            if getattr(self.executor, "is_real", False):
                self._events.publish(EventType.DEVICE_CONNECTING, f"正在连接设备 {self.executor.device_serial}")
            address = self.executor.prepare(self.stop_event)
            if address:
                self.device_changed.emit(address, "已连接 · 实机执行")
                self._events.publish(EventType.DEVICE_CONNECTED, "设备连接成功", "SUCCESS")
            for index, task in enumerate(self.queue):
                if self.stop_event.is_set():
                    raise ExecutionStopped()
                current = task.name
                current_entry = task.entry
                self._business.set_task(task)
                task_start = monotonic()
                self._events.publish(EventType.TASK_STARTED, f"开始任务：{task.name}",
                                     task_name=task.name, task_entry=task.entry)
                self.task_started.emit(task.name)
                try:
                    self.executor.execute(task, self.stop_event)
                except ExecutionStopped:
                    raise
                except Exception as exc:
                    if self.stop_event.is_set():
                        raise ExecutionStopped() from None
                    if not self.continue_on_failure:
                        raise
                    logger.exception("%s失败，已启用继续后续任务", current)
                    self.problem_reported.emit(describe_failure(exc, stage="task", task_name=current))
                    failed.add(task.entry)
                    self._events.publish(
                        EventType.TASK_FAILED, f"{current}失败：{exc}；保留失败结果，确认安全恢复后才继续队列。",
                        "ERROR", current, task_entry=current_entry, elapsed_seconds=monotonic() - task_start,
                    )
                    task_start = None
                    self._business.set_task(None)
                    if index + 1 < len(self.queue):
                        self.state_changed.emit("单项任务失败，正在确认返回主页…")
                        self._events.publish(EventType.TASK_PROGRESS, "正在恢复主页，确认后继续下一项任务。", "WARNING")
                        recover = getattr(self.executor, "recover_after_failure", None)
                        try:
                            ready = (recover(self.stop_event) if callable(recover)
                                     else not getattr(self.executor, "is_real", False))
                        except ExecutionStopped:
                            raise
                        except Exception as exc:
                            raise QueueRecoveryFailed("返回主页失败，已停止后续任务；详情见详细日志。") from exc
                        if self.stop_event.is_set():
                            raise ExecutionStopped()
                        if not ready:
                            raise QueueRecoveryFailed("无法确认安全返回主页，已停止后续任务；详情见详细日志。")
                        self._events.publish(EventType.TASK_PROGRESS, "已确认返回主页，继续下一项任务。")
                    continue
                elapsed = monotonic() - task_start
                self._events.publish(EventType.TASK_COMPLETED, f"完成任务：{task.name}（用时 {elapsed:.1f} 秒）",
                                     "SUCCESS", task.name, task_entry=task.entry, elapsed_seconds=elapsed)
                completed.add(task.entry)
                task_start = None
                self._business.set_task(None)
            if self.stop_event.is_set():
                raise ExecutionStopped()
            if failed:
                succeeded_count, failed_count, _ = counts()
                state, message = "partial", f"队列执行结束：成功 {succeeded_count} 项，失败 {failed_count} 项。"
        except ExecutionStopped:
            logger.info("任务队列已停止")
            state, message = "stopped", "已停止"
            if task_start is not None:
                elapsed = monotonic() - task_start
                self._events.publish(EventType.TASK_STOPPED, f"任务中断：{current}（用时 {elapsed:.1f} 秒）",
                                     "WARNING", current, task_entry=current_entry, elapsed_seconds=elapsed)
            self._events.publish(EventType.QUEUE_INTERRUPTED, f"后续任务已中断；未完成 {counts()[2]} 项", "WARNING")
        except QueueRecoveryFailed as exc:
            logger.exception("失败后的主页恢复未完成")
            state, message = "error", str(exc)
            self.problem_reported.emit(describe_failure(exc, stage="recovery"))
            self._events.publish(EventType.TASK_PROGRESS, message, "ERROR")
            self._events.publish(EventType.QUEUE_INTERRUPTED,
                                 f"后续任务已中断；未执行 {counts()[2]} 项", "WARNING")
        except Exception as exc:
            logger.exception("%s失败", current)
            state = "error"
            self.problem_reported.emit(describe_failure(
                exc, stage="prepare" if task_start is None else "task", task_name=current))
            # 原始异常/traceback 留在详细文件，普通 GUI 不显示内部节点或对象 repr。
            if task_start is None:
                message = "设备连接或运行环境初始化失败，请检查连接设置；详情见详细日志。"
                self._events.publish(EventType.DEVICE_FAILED, message, "ERROR")
            else:
                message = f"{current}失败，已停止后续任务；详情见详细日志。"
                self._events.publish(EventType.TASK_FAILED, message, "ERROR", current,
                                     task_entry=current_entry, elapsed_seconds=monotonic() - task_start)
            self._events.publish(EventType.QUEUE_INTERRUPTED, f"后续任务已中断；未完成 {counts()[2]} 项", "WARNING")
        finally:
            self._business.set_task(None)
            try:
                if getattr(self.executor, "is_real", False):
                    self.state_changed.emit("正在安全停止…" if self.stop_event.is_set() else "正在释放运行环境…")
                self.executor.close()
                self._events.publish(EventType.RUNTIME_RELEASED, "运行环境已释放", "SUCCESS")
            except Exception as exc:
                logger.exception("运行环境释放失败")
                self.problem_reported.emit(describe_failure(exc, stage="cleanup"))
                state, message = "error", "运行环境释放失败；详情见详细日志。"
                self._events.publish(EventType.TASK_FAILED, message, "ERROR")
            if getattr(self.executor, "is_real", False):
                self.device_changed.emit(self.executor.device_serial, "运行环境已释放" if state != "error" else "运行环境已结束 / 错误")
        return state, message

"""只转发日志，不改变任务结果、识别或任何业务控制流。"""
from __future__ import annotations

from app_paths import DATA_ROOT

import logging
import json
import uuid
from pathlib import Path
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from threading import RLock, get_ident

MAX_LINE_LENGTH = 4000
_capture_lock = RLock()
_ansi = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def local_timestamp() -> str:
    # 客户端本地时区 Asia/Shanghai；Windows 无需额外安装 tzdata。
    return datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M:%S")

class LineTee:
    """保留原 stream；按线程拼接 print 的碎片，避免 Maa 回调日志串行混拼。"""

    def __init__(self, original, emit, default_level="INFO", details=None):
        self.original = original
        self.emit = emit
        self.default_level = default_level
        self.details = details
        self._buffers = {}
        self._lock = RLock()

    def write(self, value):
        with self._lock:
            result = self.original.write(value)
            if self.details is not None:
                self.details.write(value)
            key = get_ident()
            text = self._buffers.pop(key, "") + value
            parts = text.split("\n")
            for line in parts[:-1]:
                self._forward(line)
            tail = parts[-1]
            if len(tail) >= MAX_LINE_LENGTH:
                self._forward(tail[:MAX_LINE_LENGTH])
                tail = ""
            if tail:
                self._buffers[key] = tail
            return result

    def _forward(self, line):
        line = _ansi.sub("", line).strip("\r")
        if line.strip():
            self.emit(self.default_level, line)

    def flush(self):
        self.original.flush()

    def finish(self):
        with self._lock:
            for line in self._buffers.values():
                self._forward(line)
            self._buffers.clear()

    def __getattr__(self, name):
        return getattr(self.original, name)


class DetailedLog:
    """原始输出完整落盘；GUI 的 2000 条上限不应用于该文件。"""
    def __init__(self, path=None):
        if path is None:
            from gui.task_registry import ROOT
            stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")
            path = DATA_ROOT / "images" / "debug" / "gui" / f"runtime-{stamp}-{uuid.uuid4().hex[:8]}.log"
        self.path = Path(path)
        self.lock = RLock()
        self.error = None
        self.stream = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.stream = self.path.open("a", encoding="utf-8", errors="backslashreplace")
        except OSError as exc:
            self.error = str(exc)

    def write(self, text):
        with self.lock:
            if self.stream is not None and self.error is None:
                try:
                    self.stream.write(text)
                    self.stream.flush()
                except OSError as exc:
                    self.error = str(exc)

    def record(self, level, text):
        self.write(f"\n{local_timestamp()}  [{level}]  {text}\n")

    def close(self):
        with self.lock:
            if self.stream is not None:
                try:
                    self.stream.close()
                except OSError as exc:
                    self.error = str(exc)
                self.stream = None


class ForwardHandler(logging.Handler):
    def __init__(self, details):
        super().__init__(logging.NOTSET)
        self.details = details
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s %(message)s"))

    def emit(self, record):
        # formatter 保留 exception/stack_info；不把 logging record 发到 GUI。
        self.details.write(self.format(record) + "\n")


@contextmanager
def forward_output(observe, path=None):
    """原始文本写详细文件；observe 仅供已审计的业务事件适配器使用。"""
    with _capture_lock:
        details = DetailedLog(path)
        stdout, stderr = sys.stdout, sys.stderr
        out, err = LineTee(stdout, observe, details=details), LineTee(stderr, observe, "ERROR", details)
        handler = ForwardHandler(details)
        root = logging.getLogger()
        sys.stdout, sys.stderr = out, err
        root.addHandler(handler)
        try:
            yield details
        finally:
            root.removeHandler(handler)
            sys.stdout, sys.stderr = stdout, stderr
            out.finish()
            err.finish()
            handler.close()
            details.close()


def attach_framework_logs(tasker, emit, user_observer=None) -> tuple[tuple[str, int], ...]:
    """原始回调写详细文件；只有白名单节点结果可生成用户事件。"""
    from maa.context import ContextEventSink
    from maa.tasker import TaskerEventSink

    def raw(message, details):
        emit("DEBUG", "[MaaFramework] " + json.dumps({"message": message, "details": details}, ensure_ascii=False, default=str))
        if user_observer is not None:
            user_observer(message, details)

    class FocusSink(ContextEventSink):
        def on_raw_notification(self, context, msg, details):
            raw(msg, details)

    class TaskSink(TaskerEventSink):
        def on_raw_notification(self, tasker, msg, details):
            raw(msg, details)

    ids = []
    try:
        context_id = tasker.add_context_sink(FocusSink())
        if context_id is not None:
            ids.append(("context", context_id))
        task_id = tasker.add_sink(TaskSink())
        if task_id is not None:
            ids.append(("task", task_id))
    except Exception as exc:
        # 监听失败只影响日志，不改变已有任务行为。
        emit("WARNING", f"MaaFramework 事件日志订阅失败：{exc}")
    return tuple(ids)


def detach_framework_logs(tasker, ids, emit):
    for kind, sink_id in ids:
        try:
            if kind == "context":
                tasker.remove_context_sink(sink_id)
            else:
                tasker.remove_sink(sink_id)
        except Exception as exc:
            emit("WARNING", f"MaaFramework 日志监听释放失败：{exc}")

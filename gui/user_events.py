"""GUI 唯一的用户日志契约；原始文本不是用户事件。"""
from dataclasses import dataclass
from enum import Enum
from threading import RLock

from gui.log_bridge import local_timestamp


class EventType(str, Enum):
    DEVICE_CONNECTING = "DEVICE_CONNECTING"
    DEVICE_CONNECTED = "DEVICE_CONNECTED"
    DEVICE_FAILED = "DEVICE_FAILED"
    TASK_STARTED = "TASK_STARTED"
    TASK_PROGRESS = "TASK_PROGRESS"
    TASK_SKIPPED = "TASK_SKIPPED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TASK_STOPPED = "TASK_STOPPED"
    QUEUE_STARTED = "QUEUE_STARTED"
    QUEUE_INTERRUPTED = "QUEUE_INTERRUPTED"
    QUEUE_COMPLETED = "QUEUE_COMPLETED"
    QUEUE_FINISHED = "QUEUE_FINISHED"
    STOP_REQUESTED = "STOP_REQUESTED"
    STOP_COMPLETED = "STOP_COMPLETED"
    RUNTIME_RELEASED = "RUNTIME_RELEASED"


@dataclass(frozen=True)
class UserEvent:
    kind: EventType
    time: str
    level: str
    text: str
    task_name: str | None = None
    task_entry: str | None = None
    elapsed_seconds: float | None = None


class UserEvents:
    def __init__(self, callback):
        self.callback = callback
        self.lock = RLock()

    def publish(self, kind: EventType, text: str, level="INFO", task_name=None,
                *, task_entry=None, elapsed_seconds=None):
        if not isinstance(kind, EventType):
            raise TypeError("必须提供明确的用户事件类型")
        with self.lock:
            self.callback(UserEvent(kind, local_timestamp(), level, text, task_name,
                                    task_entry, elapsed_seconds))

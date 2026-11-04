from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, Sequence, TypeVar

from qjnn_alliance_commission import OcrItem
from qjnn_common_popup import (
    GlobalTransientResult,
    handle_transient_if_present,
)


StateT = TypeVar("StateT")


@dataclass(frozen=True)
class TaskStateSnapshot(Generic[StateT]):
    state: StateT
    frame: object
    items: list[OcrItem]


def detect_entry_state(
    session,
    detector: Callable[[Sequence[OcrItem], object], StateT | None],
    *,
    task_name: str,
    loading_classifier: Callable[[Sequence[OcrItem], object], bool] | None = None,
    loading_timeout: float = 20.0,
    loading_interval: float = 0.5,
    capture_unknown: Callable[[str, object], bool] | None = None,
    unknown_capture_state: str,
    loading_timeout_state: str | None = None,
    log: Callable[[str], None] = print,
) -> TaskStateSnapshot[StateT] | None:
    """Detect a restart state without resetting the task to HOME.

    The task detector always gets the first chance.  Only a business miss may
    invoke the existing global transient layer.  A true unknown is diagnostic
    only and never causes a click or a Back action.
    """

    frame = session.capture_frame()
    items = list(session.recognize_text(frame))
    state = detector(items, frame)
    if state is not None:
        log(f"[StateRecovery] task={task_name}, state={state}")
        return TaskStateSnapshot(state, frame, items)

    transient, fresh_frame, fresh_items = handle_transient_if_present(
        session,
        frame,
        items,
        loading_classifier=loading_classifier,
        loading_timeout=loading_timeout,
        loading_interval=loading_interval,
        loading_timeout_state=loading_timeout_state or unknown_capture_state,
        loading_capture=capture_unknown,
        log=log,
    )
    if transient in {
        GlobalTransientResult.HANDLED_POPUP,
        GlobalTransientResult.WAITED_LOADING,
    }:
        refreshed_items = list(fresh_items)
        refreshed_state = detector(refreshed_items, fresh_frame)
        if refreshed_state is not None:
            log(
                f"[StateRecovery] task={task_name}, "
                f"transient={transient.value}, state={refreshed_state}"
            )
            return TaskStateSnapshot(refreshed_state, fresh_frame, refreshed_items)
        frame, items = fresh_frame, refreshed_items

    if capture_unknown is not None:
        capture_unknown(unknown_capture_state, frame)
    log(f"[StateRecovery] task={task_name}, state=UNKNOWN; zero clicks")
    for item in items:
        log(
            f"[StateRecovery] OCR text={item.text!r}, "
            f"confidence={float(item.score):.6f}, box="
            f"({item.box.x},{item.box.y},{item.box.width},{item.box.height})"
        )
    return None

"""Maa task controller with the shared Python ADB one-shot recovery policy."""
from __future__ import annotations

import numpy as np
from maa.controller import AdbController as MaaAdbController, CustomController


class RecoveringController(CustomController):
    def __init__(self, adb, *, native_factory=MaaAdbController):
        self.adb = adb
        self._native_factory = native_factory
        self._native = None
        self._connected = False
        self._uuid = ""
        self.last_error = ""
        self.input_uncertain = False
        self.privacy_guard = None
        super().__init__()

    def _connect_native(self):
        native = self._native_factory(str(self.adb.adb_path), self.adb.serial)
        # Outer Maa controller owns screenshot scaling and maps input to raw
        # coordinates. Inner controller must stay raw to avoid double scaling.
        if not native.set_screenshot_use_raw_size(True):
            raise RuntimeError("could not enable native raw screenshot mode")
        job = native.post_connection().wait()
        if not job.succeeded or not native.connected:
            raise RuntimeError("native Maa controller connection failed")
        self._native = native
        if not self._uuid:
            self._uuid = native.uuid
        self._connected = True

    def _call(self, operation, failure=False, *, retry=True):
        try:
            return operation()
        except Exception as first:
            if not retry:
                # A failed acknowledgement does not prove input was undelivered.
                # Never replay old coordinates or admit further input this run.
                self.input_uncertain = True
                self._connected = False
                self.last_error = str(first)
                print(f"[RecoveringController] input result uncertain; no replay: {first}")
                return failure
            try:
                if not self.adb.recover_after_controller_failure():
                    raise RuntimeError("no successful disconnect recovery") from first
                self._connect_native()
                # No recursive call: a second failure never gets another retry.
                return operation()
            except Exception as exc:
                self._connected = False
                self.last_error = str(exc)
                print(f"[RecoveringController] operation failed: {first}; recovery: {exc}")
                return failure

    def connect(self):
        def operation():
            self._connect_native()
            return True
        return self._call(operation)

    def connected(self):
        return self._connected

    def request_uuid(self):
        return self._uuid

    def get_features(self):
        # Use the native single-click/swipe path, not synthetic touch sequences.
        return 0

    def get_custom_info(self):
        return {"adb_path": str(self.adb.adb_path), "adb_serial": self.adb.serial,
                "profile": self.adb.profile.name}

    def _job(self, method, *args):
        if self.input_uncertain:
            return False
        def operation():
            if self.privacy_guard is not None and not self.privacy_guard.allow_input(method, self._native):
                return False
            job = getattr(self._native, method)(*args).wait()
            if not job.succeeded:
                raise RuntimeError(f"native {method} failed")
            return True
        return self._call(operation, retry=False)

    def screencap(self):
        def operation():
            if self.privacy_guard is not None:
                frame = self.privacy_guard.capture(self._native)
                if frame is None:
                    return np.empty((0, 0, 3), dtype=np.uint8)
                return frame
            job = self._native.post_screencap().wait()
            if not job.succeeded:
                raise RuntimeError("native screenshot failed")
            frame = self._native.cached_image
            if frame is None or not getattr(frame, "size", 0):
                raise RuntimeError("native screenshot empty")
            return frame
        # Empty frames fail Maa's screenshot job instead of exposing stale data.
        return self._call(operation, np.empty((0, 0, 3), dtype=np.uint8))

    def click(self, x, y):
        return self._job("post_click", x, y)

    def swipe(self, x1, y1, x2, y2, duration):
        return self._job("post_swipe", x1, y1, x2, y2, duration)

    def start_app(self, intent):
        return self._job("post_start_app", intent)

    def stop_app(self, intent):
        return self._job("post_stop_app", intent)

    def click_key(self, keycode):
        return self._job("post_click_key", keycode)

    def input_text(self, text):
        return self._job("post_input_text", text)

    def touch_down(self, contact, x, y, pressure):
        return self._job("post_touch_down", x, y, contact, pressure)

    def touch_move(self, contact, x, y, pressure):
        return self._job("post_touch_move", x, y, contact, pressure)

    def touch_up(self, contact):
        return self._job("post_touch_up", contact)

    def key_down(self, keycode):
        return self._job("post_key_down", keycode)

    def key_up(self, keycode):
        return self._job("post_key_up", keycode)

    def shell(self, cmd, timeout):
        if self.input_uncertain:
            return None
        def operation():
            job = self._native.post_shell(cmd, timeout).wait()
            if not job.succeeded:
                raise RuntimeError("native shell failed")
            return self._native.shell_output
        return self._call(operation, None, retry=False)

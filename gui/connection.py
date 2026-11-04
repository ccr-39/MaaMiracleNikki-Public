"""Device discovery and read-only checks outside the GUI thread."""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from gui.config import GuiConfig


def inspect_connection(config: GuiConfig, *, test: bool = False) -> dict:
    from tools import run_task  # noqa: F401 -- shared agent import path
    import device_manager as dm
    adb = dm.resolve_adb_path(config.adb_path or None, config=dm.load_connection_config())
    address = config.adb_address.strip()
    if test and not address:
        raise ValueError("请先填写连接地址，或从检测到的设备中选择。")
    if test and config.auto_connect and dm.is_tcp_serial(address):
        if not dm.connect_endpoint(adb, address):
            raise ValueError(f"无法连接 {address}；请检查模拟器的 ADB 端口和调试开关。")
    devices = dm.list_devices(adb)
    result = {"adb_path": str(adb), "devices": devices}
    if not test:
        return result
    device = next((device for device in devices if device.serial == address), None)
    if device is None or device.state != "device":
        state = device.state if device is not None else "未发现"
        raise ValueError(f"设备 {address} 不可用（{state}）；请启动模拟器、授权 ADB，或勾选自动连接。")
    controller = dm.AdbController(adb_path=adb, serial=address, profile=config.connection_profile)
    _, android, model = controller.validate_connection()
    png = controller.screencap()
    import cv2
    import numpy as np
    frame = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("截图解码失败，请检查所选 ADB 和模拟器。")
    result.update(address=address, model=model, android=android, png=png,
                  width=frame.shape[1], height=frame.shape[0])
    return result


class ConnectionWorker(QThread):
    result_ready = Signal(object)
    failed = Signal(str)

    def __init__(self, config: GuiConfig, *, test: bool, parent=None):
        super().__init__(parent)
        self.config, self.test = config, test

    def run(self):
        try:
            self.result_ready.emit(inspect_connection(self.config, test=self.test))
        except Exception as exc:
            self.failed.emit(str(exc))

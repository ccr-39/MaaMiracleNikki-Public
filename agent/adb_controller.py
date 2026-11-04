from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


DEFAULT_PACKAGE_NAME = "com.tencent.tmgp.qjnn"
DISCONNECT_MARKERS = (
    "device not found",
    "device offline",
    "no devices/emulators found",
    "device '" ,
)


class AdbError(RuntimeError):
    """An ADB operation failed with a concise, user-readable message."""


@dataclass(frozen=True)
class AndroidDevice:
    serial: str
    state: str
    model: str = ""
    product: str = ""
    device: str = ""
    transport_id: str = ""


class ConnectionProfile:
    """General ADB commands. Emulator profiles may override only quirks."""

    name = "General"

    def screencap_args(self) -> list[str]:
        return ["exec-out", "screencap", "-p"]

    def tap_args(self, x: int, y: int) -> list[str]:
        return ["shell", "input", "tap", str(int(x)), str(int(y))]

    def swipe_args(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int,
    ) -> list[str]:
        return [
            "shell",
            "input",
            "swipe",
            str(int(start_x)),
            str(int(start_y)),
            str(int(end_x)),
            str(int(end_y)),
            str(int(duration_ms)),
        ]


class MuMuProfile(ConnectionProfile):
    name = "MuMu"


PROFILES: dict[str, type[ConnectionProfile]] = {
    "General": ConnectionProfile,
    "MuMu": MuMuProfile,
}


def get_profile(name: str) -> ConnectionProfile:
    profile_type = PROFILES.get(name)
    if profile_type is None:
        raise ValueError(f"未知连接 profile：{name}")
    return profile_type()


def run_process(
    command: Sequence[str],
    *,
    binary: bool = False,
    timeout: float = 15,
) -> subprocess.CompletedProcess:
    text_options = (
        {"text": False}
        if binary
        else {"text": True, "encoding": "utf-8", "errors": "replace"}
    )
    try:
        return subprocess.run(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            **text_options,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdbError(f"ADB 命令超时（{timeout:g} 秒）。") from exc
    except OSError as exc:
        raise AdbError(f"无法执行 ADB：{exc}") from exc


RecoveryCallback = Callable[[str], AndroidDevice | None]


class AdbController:
    """One immutable ADB executable plus one selected Android serial."""

    def __init__(
        self,
        *,
        adb_path: str | Path,
        serial: str,
        profile: str = "General",
        model: str = "",
        package_name: str = DEFAULT_PACKAGE_NAME,
        recovery_callback: RecoveryCallback | None = None,
    ) -> None:
        if not serial.strip():
            raise ValueError("serial 不能为空")
        self.adb_path = Path(adb_path).resolve()
        self.serial = serial.strip()
        self.profile = get_profile(profile)
        self.model = model
        self.package_name = package_name
        self._recovery_callback = recovery_callback
        self._recovery_used = False

    def _command(self, args: Sequence[str]) -> list[str]:
        return [str(self.adb_path), "-s", self.serial, *args]

    @staticmethod
    def _message(result: subprocess.CompletedProcess) -> str:
        stderr = result.stderr
        stdout = result.stdout
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        return str(stderr or stdout or "未知 ADB 错误").strip()

    @staticmethod
    def _is_disconnect(message: str) -> bool:
        lowered = message.lower()
        return any(marker in lowered for marker in DISCONNECT_MARKERS)

    def run_adb(
        self,
        *args: str,
        binary: bool = False,
        timeout: float = 15,
    ) -> str | bytes:
        result = run_process(self._command(args), binary=binary, timeout=timeout)
        if result.returncode == 0:
            return result.stdout

        message = self._message(result)
        if (
            not self._recovery_used
            and self._recovery_callback is not None
            and self._is_disconnect(message)
        ):
            self._recovery_used = True
            recovered = self._recovery_callback(self.serial)
            if recovered is not None:
                self.serial = recovered.serial
                self.model = recovered.model or self.model
                retry = run_process(self._command(args), binary=binary, timeout=timeout)
                if retry.returncode == 0:
                    return retry.stdout
                message = self._message(retry)

        raise AdbError(f"设备 {self.serial} 执行 ADB 失败：{message}")

    def shell(self, *args: str, timeout: float = 15) -> str:
        output = self.run_adb("shell", *args, timeout=timeout)
        return str(output).strip()

    def recover_after_controller_failure(self) -> bool:
        """Probe a failed native operation through the existing one-shot recovery.

        A healthy ADB probe is not grounds to replay an ambiguous failed click.
        Only a detected disconnect followed by successful recovery permits retry.
        """
        if self._recovery_used:
            return False
        self.shell("true", timeout=5)
        return self._recovery_used

    def screencap(self) -> bytes:
        output = self.run_adb(*self.profile.screencap_args(), binary=True, timeout=30)
        if not isinstance(output, bytes) or not output.startswith(b"\x89PNG\r\n\x1a\n"):
            raise AdbError(f"设备 {self.serial} 返回的截图不是有效 PNG。")
        return output

    def screenshot(self, destination: Path) -> None:
        png = self.screencap()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(png)

    def tap(self, x: int, y: int) -> None:
        self.run_adb(*self.profile.tap_args(x, y))

    click = tap

    def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 400,
    ) -> None:
        self.run_adb(
            *self.profile.swipe_args(
                start_x, start_y, end_x, end_y, duration_ms
            )
        )

    def input_text(self, text: str) -> None:
        self.shell("input", "text", text)

    input = input_text

    def start_app(self, package_name: str | None = None) -> None:
        package = package_name or self.package_name
        self.shell(
            "am",
            "start",
            "-a",
            "android.intent.action.MAIN",
            "-c",
            "android.intent.category.LAUNCHER",
            "-p",
            package,
        )

    def stop_app(self, package_name: str | None = None) -> None:
        self.shell("am", "force-stop", package_name or self.package_name)

    def validate_connection(self) -> tuple[str, str, str]:
        android_id = self.shell("settings", "get", "secure", "android_id")
        android_version = self.shell("getprop", "ro.build.version.release")
        model = self.shell("getprop", "ro.product.model") or self.model
        if not android_id or android_id.lower() in {"null", "unknown"}:
            raise AdbError(f"设备 {self.serial} 未返回有效 android_id。")
        self.model = model
        return android_id, android_version, model

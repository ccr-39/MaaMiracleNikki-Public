"""Start only the configured emulator and wait for the exact ADB endpoint."""
import os
import json
import re
from pathlib import Path
import shlex
import subprocess
import logging
from threading import Lock, Thread
from time import monotonic

from gui.branding import GUI_LOGGER_NAME


def _diagnostic_text(value, *, tail=False):
    """Keep diagnostics single-line and mask common credentials/user directories."""
    text = str(value)
    text = re.sub(r'(?i)(--?(?:password|passwd|token|secret|api[-_]?key|authorization)\s*[=:]?\s*)("[^"]*"|\'[^\']*\'|[^\s]+)',
                  r'\1<redacted>', text)
    text = re.sub(r'''(?i)(\b(?:password|passwd|token|secret|api[-_]?key|authorization)["']?\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,}]+)''',
                  r'\1<redacted>', text)
    text = re.sub(r'(?i)(https?://)[^\s/@]+:[^\s/@]+@', r'\1<redacted>@', text)
    text = re.sub(r'(?i)[A-Z]:[\\/]Users[\\/][^\\/\s"]+', '<USERPROFILE>', text)
    text = text.replace(str(Path.home()), '<USERPROFILE>')
    text = ' '.join(text.split())
    return text[-2000:] if tail else text[:2000]


class _LauncherOutput:
    """Drain both output streams without pipe backpressure or unbounded storage.

    A launcher can hand its output handle to a long-lived child. The daemon
    reader therefore never waits for EOF on the GUI worker or kills a process.
    """
    def __init__(self, stream):
        self.tail = bytearray()
        self.lock = Lock()
        self.thread = Thread(target=self._drain, args=(stream,), daemon=True)
        self.thread.start()

    def _drain(self, stream):
        try:
            with stream:
                while chunk := stream.read1(4096):
                    with self.lock:
                        self.tail.extend(chunk)
                        del self.tail[:-8192]
        except (OSError, ValueError):
            pass  # Output is best-effort; process status remains authoritative.

    def snapshot(self, *, exited=False):
        if exited:
            self.thread.join(timeout=0.05)
        with self.lock:
            data = bytes(self.tail)
        try:
            return _diagnostic_text(data.decode('utf-8'), tail=True)
        except UnicodeDecodeError:
            return _diagnostic_text(data.decode('gb18030', errors='replace'), tail=True)


def resolve_launch(path, arguments, serial):
    """Resolve blank MuMu launch arguments from its local ADB port mapping.

    Read only the port field; do not retain Android or game identifiers.
    Explicit user-supplied arguments and other emulator commands are preserved.
    """
    if arguments.strip() or path.name.lower() not in {"mumunxmain.exe", "mumu-cli.exe"}:
        return path, arguments, None
    host, separator, port = serial.rpartition(":")
    if not separator or host not in {"127.0.0.1", "localhost", "[::1]"} or not port.isdigit():
        raise ValueError("MuMu 自动启动需要本机 ADB 地址，或手动填写实例启动参数")
    matches = []
    for config in (path.parent.parent / "vms").glob("MuMuPlayer-*/configs/vm_config.json"):
        match = re.fullmatch(r"MuMuPlayer-(12|15)(?:\.\d+)*-(\d+)", config.parent.parent.name)
        if match is None:
            continue
        try:
            value = json.loads(config.read_text(encoding="utf-8-sig"))
            adb_port = value["vm"]["nat"]["port_forward"]["adb"]["host_port"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if str(adb_port) == port:
            matches.append((match.group(1), match.group(2)))
    cli = path.parent / "mumu-cli.exe"
    if len(matches) != 1 or not cli.is_file():
        raise ValueError("未能按 ADB 地址唯一匹配 MuMu 实例，请检查地址或填写实例启动参数")
    version, index = matches[0]
    return cli, f"control --vmindex {index} --version {version} launch", f"MuMu #{index}（Android {version}，ADB {serial}）"


def close_emulator(config, serial, stop, *, log):
    """Request a normal shutdown of one locally mapped MuMu instance only.

    MAA reference: src/MaaWpfGui/Helper/EmulatorHelper.cs, control shutdown.
    MuMu 15 needs an explicit Android version as well as the instance index.
    Never use launch arguments as shutdown targets or fall back to killing.
    """
    def report(text, level="INFO"):
        logging.getLogger(GUI_LOGGER_NAME).log(getattr(logging, level), text)
        log(text, level)

    try:
        if stop.is_set():
            return
        path = Path(config.emulator_path).expanduser().resolve()
        if not path.is_file() or path.name.lower() not in {"mumunxmain.exe", "mumu-cli.exe", "mumumanager.exe"}:
            raise ValueError("请在定时与启动设置中选择 MuMu 启动程序")
        # Reuse the read-only local port mapping, ignoring custom launch args.
        cli, arguments, target = resolve_launch(path.parent / "mumu-cli.exe", "", serial)
        manager = path.parent / "MuMuManager.exe"
        command = [str(manager if manager.is_file() else cli), *shlex.split(arguments)]
        command[-1] = "shutdown"
        report(f"全部任务成功，准备正常关闭 {target}；命令：{_diagnostic_text(subprocess.list2cmdline(command))}")
        if stop.is_set():
            report("已请求停止，取消关闭模拟器", "WARNING")
            return
        process = subprocess.Popen(command, cwd=str(path.parent), shell=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output = _LauncherOutput(process.stdout)
        # wait(timeout) does not terminate either the manager or emulator.
        try:
            code = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            report(f"模拟器关闭命令超时，关闭结果未确认；未终止进程；{output.snapshot()}", "WARNING")
            return
        detail = output.snapshot(exited=True) or "无输出"
        report(f"模拟器正常关闭命令{'执行成功（关闭请求已接受）' if code == 0 else '失败'}：{target}；退出码={code}；{detail}",
               "INFO" if code == 0 else "WARNING")
    except Exception as exc:
        report(f"未能关闭模拟器：{_diagnostic_text(exc)}；保留小助手窗口", "WARNING")


def ensure_emulator(config, stop, *, timeout=120, log=print):
    from tools import run_task  # shared agent import path
    import device_manager as dm
    from gui.runner import ExecutionStopped

    def report(message):
        message = '[Emulator] ' + message
        logging.getLogger(GUI_LOGGER_NAME).info(message)
        log(message)

    if stop.is_set():
        raise ExecutionStopped()
    adb = dm.resolve_adb_path(config.adb_path or None, config=dm.load_connection_config())
    serial = config.adb_address.strip()

    def ready():
        if stop.is_set():
            raise ExecutionStopped()
        if dm.is_tcp_serial(serial):
            dm.run_process([str(adb), "connect", serial], timeout=3)
        devices = dm.list_devices(adb, timeout=3)
        device = next((item for item in devices if item.serial == serial), None)
        if device is not None and device.state == "unauthorized":
            raise RuntimeError("请先在模拟器中授权 ADB 调试，再重新运行")
        if device is None or device.state != "device":
            return False
        result = dm.run_process([str(adb), "-s", serial, "shell", "getprop", "sys.boot_completed"], timeout=3)
        return result.returncode == 0 and str(result.stdout).strip() == "1"

    try:
        if ready():
            report("指定设备已经就绪，无需启动模拟器")
            return
    except dm.AdbError:
        pass
    path = Path(config.emulator_path).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() != ".exe":
        raise ValueError("请在定时与启动设置中选择有效的模拟器 exe 文件")
    path, arguments, target = resolve_launch(path, config.emulator_arguments, serial)
    if target:
        report(f"已按连接地址匹配 {_diagnostic_text(target)}")
    # Never invoke a shell; preserve native Windows quoting for instance args.
    command = (subprocess.list2cmdline([str(path)]) + " " + arguments
               if os.name == "nt" else [str(path), *shlex.split(arguments)])
    if stop.is_set():
        raise ExecutionStopped()
    report(f"启动命令：{_diagnostic_text(command)}；工作目录：{_diagnostic_text(path.parent)}")
    try:
        process = subprocess.Popen(command, executable=str(path), cwd=str(path.parent),
                                   shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as exc:
        raise RuntimeError(f"模拟器启动器创建失败：{_diagnostic_text(exc)}") from None
    deadline = monotonic() + timeout
    output = _LauncherOutput(process.stdout)
    report(f"已提交模拟器启动请求；PID={process.pid}，等待指定设备就绪（最多 {timeout:g} 秒，可停止）")
    exit_code = None

    def check_launcher():
        nonlocal exit_code
        if exit_code is None:
            exit_code = process.poll()
            if exit_code is not None:
                detail = output.snapshot(exited=True) or '未捕获到输出'
                report(f"启动器 PID={process.pid}，退出码={exit_code}；输出：{detail}")
        if exit_code is not None and exit_code != 0:
            raise RuntimeError(f"模拟器启动器失败（退出码={exit_code}）："
                               f"{output.snapshot(exited=True) or '未捕获到输出，请检查 MuMu 启动器日志'}")

    try:
        check_launcher()
        while monotonic() < deadline:
            if stop.wait(min(2, max(0, deadline - monotonic()))):
                raise ExecutionStopped()
            check_launcher()
            try:
                if ready():
                    report("模拟器启动成功：指定 ADB 设备已经就绪")
                    return
            except dm.AdbError:
                continue
        check_launcher()
        status = f'退出码={exit_code}' if exit_code is not None else '仍在运行，退出码未定'
        raise RuntimeError(f"模拟器启动等待超时：启动请求已提交，指定设备 {_diagnostic_text(serial)} 未就绪；"
                           f"启动器 PID={process.pid}，{status}；"
                           "请检查 MuMu 实例是否卡在 stopping（正在停止）、是否成功打开窗口及 ADB 地址；"
                           "启动器退出码为 0 不代表设备启动成功")
    finally:
        if exit_code is None:
            exit_code = process.poll()
            status = f'退出码={exit_code}' if exit_code is not None else '仍在运行，退出码未定'
            report(f"启动器 PID={process.pid}，{status}；未终止启动器或模拟器")
        detail = output.snapshot(exited=exit_code is not None)
        if detail:
            report(f"启动器输出（最多 2000 字符，尾部采集）：{detail}")

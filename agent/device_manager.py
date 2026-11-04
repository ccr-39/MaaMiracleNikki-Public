from __future__ import annotations

from app_paths import DATA_ROOT

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from adb_controller import (
    DEFAULT_PACKAGE_NAME,
    AdbController,
    AdbError,
    AndroidDevice,
    run_process,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = DATA_ROOT / "config" / "device.json"
MUMU_ADB_CANDIDATES = (
    Path(r"D:\MuMuPlayer\MuMuPlayer\nx_device\15.0\shell\adb.exe"),
    Path(r"C:\Program Files\Netease\MuMuPlayerGlobal-12.0\shell\adb.exe"),
)
BUNDLED_ADB_CANDIDATES = (
    PROJECT_ROOT / "adb.exe",
    PROJECT_ROOT / "platform-tools" / "adb.exe",
    PROJECT_ROOT / "tools" / "adb.exe",
)
MUMU_PRIMARY_TCP_ENDPOINT = "127.0.0.1:16416"


class DeviceSelectionError(AdbError):
    pass


@dataclass(frozen=True)
class DeviceIdentity:
    android_id: str
    model: str
    product: str
    device: str
    ro_serialno: str = ""


_INVALID_DEVICE_IDENTIFIERS = {"", "null", "unknown"}


def load_connection_config(config_path: Path = DEFAULT_CONFIG_PATH) -> dict[str, object]:
    if not config_path.is_file():
        return {}
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeviceSelectionError(f"无法读取设备配置 {config_path}：{exc}") from exc
    if not isinstance(value, dict):
        raise DeviceSelectionError(f"设备配置必须是 JSON object：{config_path}")
    return value


def _valid_file(value: object) -> Path | None:
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        return None
    path = Path(value).expanduser()
    return path.resolve() if path.is_file() else None


def resolve_adb_path(
    explicit_adb_path: str | Path | None = None,
    *,
    config: dict[str, object] | None = None,
    project_adb_candidates: Iterable[str | Path] = (),
) -> Path:
    """Resolve exactly one executable and keep it for the whole run."""
    print("[ADB] Resolving adb executable...")
    # An explicit GUI/CLI path must never silently fall back to another ADB.
    if explicit_adb_path is not None and str(explicit_adb_path).strip():
        value = os.path.expandvars(str(explicit_adb_path).strip().strip('"'))
        candidate = _valid_file(value if Path(value).is_absolute() else PROJECT_ROOT / value)
        if candidate is None:
            candidate = _valid_file(shutil.which(value))
        if candidate is None:
            raise AdbError(f"指定的 ADB 不存在：{explicit_adb_path}；请在连接设置中重新选择。")
        return candidate
    values: list[object] = [explicit_adb_path]
    if config:
        values.append(config.get("adb_path"))
    values.extend(project_adb_candidates)
    values.extend(MUMU_ADB_CANDIDATES)

    path_adb = shutil.which("adb")
    if path_adb:
        values.append(path_adb)
    values.extend(BUNDLED_ADB_CANDIDATES)

    for value in values:
        candidate = _valid_file(value)
        if candidate is not None:
            print(f"[ADB] adb = {candidate}")
            return candidate
    raise AdbError(
        "未找到可用 ADB。请在设置 → 连接设置中选择模拟器自带的 adb.exe，"
        "或将 platform-tools 放到项目目录。"
    )


def parse_devices_output(output: str) -> list[AndroidDevice]:
    devices: list[AndroidDevice] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices attached") or line.startswith("*"):
            continue
        columns = line.split()
        if len(columns) < 2:
            continue
        metadata: dict[str, str] = {}
        for item in columns[2:]:
            if ":" in item:
                key, value = item.split(":", 1)
                metadata[key] = value
        devices.append(
            AndroidDevice(
                serial=columns[0],
                state=columns[1],
                model=metadata.get("model", ""),
                product=metadata.get("product", ""),
                device=metadata.get("device", ""),
                transport_id=metadata.get("transport_id", ""),
            )
        )
    return devices


def list_devices(adb_path: str | Path, *, timeout: float = 15) -> list[AndroidDevice]:
    result = run_process([str(adb_path), "devices", "-l"], timeout=timeout)
    if result.returncode != 0:
        message = str(result.stderr or result.stdout or "未知错误").strip()
        raise AdbError(f"adb devices -l 失败：{message}")
    return parse_devices_output(str(result.stdout))


def _device_controller(
    adb_path: str | Path,
    device: AndroidDevice,
    *,
    profile: str,
    package_name: str,
) -> AdbController:
    return AdbController(
        adb_path=adb_path,
        serial=device.serial,
        profile=profile,
        model=device.model,
        package_name=package_name,
    )


def _normalized_identifier(value: str) -> str:
    normalized = value.strip()
    return "" if normalized.lower() in _INVALID_DEVICE_IDENTIFIERS else normalized


def query_device_identity(
    adb_path: str | Path,
    device: AndroidDevice,
    *,
    profile: str,
) -> DeviceIdentity | None:
    """Read a conservative identity used only for alias deduplication."""
    controller = _device_controller(
        adb_path,
        device,
        profile=profile,
        package_name=DEFAULT_PACKAGE_NAME,
    )
    try:
        android_id = _normalized_identifier(
            controller.shell("settings", "get", "secure", "android_id")
        )
    except AdbError as exc:
        print(f"[ADB] Identity query failed for {device.serial}: {exc}")
        return None
    if not android_id:
        print(f"[ADB] Identity query failed for {device.serial}: invalid android_id")
        return None

    def optional_prop(name: str) -> str:
        try:
            return _normalized_identifier(controller.shell("getprop", name))
        except AdbError:
            return ""

    identity = DeviceIdentity(
        android_id=android_id,
        model=_normalized_identifier(device.model) or optional_prop("ro.product.model"),
        product=_normalized_identifier(device.product) or optional_prop("ro.product.name"),
        device=_normalized_identifier(device.device) or optional_prop("ro.product.device"),
        ro_serialno=optional_prop("ro.serialno"),
    )
    if not all((identity.model, identity.product, identity.device)):
        print(
            f"[ADB] Identity query incomplete for {device.serial}; "
            "alias merge disabled"
        )
        return None
    return identity


def same_android_instance(left: DeviceIdentity, right: DeviceIdentity) -> bool:
    if left.android_id != right.android_id:
        return False
    if (left.model, left.product, left.device) != (
        right.model,
        right.product,
        right.device,
    ):
        return False
    if left.ro_serialno and right.ro_serialno:
        return left.ro_serialno == right.ro_serialno
    return True


def _canonical_alias(
    devices: list[AndroidDevice],
    *,
    configured_serial: str | None,
) -> AndroidDevice:
    primary_tcp = next(
        (
            device
            for device in devices
            if device.serial == MUMU_PRIMARY_TCP_ENDPOINT
        ),
        None,
    )
    if primary_tcp is not None:
        return primary_tcp
    if configured_serial and is_tcp_serial(configured_serial):
        configured = next(
            (device for device in devices if device.serial == configured_serial), None
        )
        if configured is not None:
            return configured
    first_alias = next(iter(devices))
    return next(
        (device for device in devices if is_tcp_serial(device.serial)),
        first_alias,
    )


def deduplicate_device_aliases(
    adb_path: str | Path,
    devices: list[AndroidDevice],
    *,
    configured_serial: str | None,
    profile: str,
) -> list[AndroidDevice]:
    """Merge online serial aliases only when their read-only identities agree."""
    identities = {
        device.serial: query_device_identity(adb_path, device, profile=profile)
        for device in devices
    }
    remaining = list(devices)
    deduplicated: list[AndroidDevice] = []
    while remaining:
        first = remaining.pop(0)
        first_identity = identities[first.serial]
        aliases = [first]
        kept: list[AndroidDevice] = []
        for candidate in remaining:
            candidate_identity = identities[candidate.serial]
            if (
                first_identity is not None
                and candidate_identity is not None
                and same_android_instance(first_identity, candidate_identity)
            ):
                aliases.append(candidate)
            else:
                kept.append(candidate)
        remaining = kept
        canonical = _canonical_alias(
            aliases,
            configured_serial=configured_serial,
        )
        deduplicated.append(canonical)
        if len(aliases) > 1:
            print("[ADB] alias merged:")
            for alias in aliases:
                print(f"  {alias.serial}")
            print(
                "[ADB] Same Android instance, canonical serial: "
                f"{canonical.serial}"
            )
    return deduplicated


def has_target_package(
    adb_path: str | Path,
    device: AndroidDevice,
    package_name: str,
    *,
    profile: str = "General",
) -> bool:
    controller = _device_controller(
        adb_path, device, profile=profile, package_name=package_name
    )
    try:
        path_output = controller.shell("pm", "path", package_name, timeout=20)
        if any(line.strip().startswith("package:") for line in path_output.splitlines()):
            return True
    except AdbError:
        pass

    try:
        packages = controller.shell("pm", "list", "packages", package_name, timeout=25)
    except AdbError:
        return False
    expected = f"package:{package_name}"
    return any(line.strip() == expected for line in packages.splitlines())


def is_tcp_serial(serial: str) -> bool:
    match = re.fullmatch(r"(?:\[[0-9A-Fa-f:]+\]|[A-Za-z0-9_.-]+):(\d+)", serial.strip())
    return match is not None and 1 <= int(match.group(1)) <= 65535


def discover_mumu_fallback_endpoints(
    configured_serial: str | None = None,
) -> list[str]:
    """Return centralized MuMu endpoints only after adb devices found none.

    An explicit TCP serial and MAAMIRACLENIKKI_MUMU_ENDPOINTS are reliable hints. The
    final two entries are legacy MuMu candidates, deliberately not port-scanned.
    """
    endpoints: list[str] = []
    if configured_serial and is_tcp_serial(configured_serial):
        endpoints.append(configured_serial)
    endpoints.extend(
        item.strip()
        # Keep the previous environment variable as a fallback for existing setups.
        for item in os.environ.get("MAAMIRACLENIKKI_MUMU_ENDPOINTS",
                                   os.environ.get("MAAQJNN_MUMU_ENDPOINTS", "")).split(",")
        if is_tcp_serial(item.strip())
    )
    endpoints.extend(("127.0.0.1:16384", "127.0.0.1:16416"))
    return list(dict.fromkeys(endpoints))


def connect_endpoint(adb_path: str | Path, endpoint: str) -> bool:
    result = run_process([str(adb_path), "connect", endpoint], timeout=15)
    message = str(result.stdout or result.stderr or "").strip()
    succeeded = result.returncode == 0 and (
        "connected to" in message.lower() or "already connected to" in message.lower()
    )
    print(f"[ADB] MuMu fallback {endpoint}: {message or 'no response'}")
    return succeeded


_FOREGROUND_PATTERNS = (
    re.compile(
        r"(?:topResumedActivity|mResumedActivity|ResumedActivity)\s*[=:]\s*"
        r".*?\bu\d+\s+([A-Za-z][A-Za-z0-9_.]*)/"
    ),
    re.compile(
        r"(?:mCurrentFocus|mFocusedApp)\s*[=:]\s*"
        r".*?\b([A-Za-z][A-Za-z0-9_.]*)/[A-Za-z0-9_.$]+"
    ),
)


def _parse_focused_package(output: str) -> str | None:
    for pattern in _FOREGROUND_PATTERNS:
        match = pattern.search(output)
        if match:
            return match.group(1)
    return None


def query_foreground_package(
    adb_path: str | Path,
    device: AndroidDevice,
    *,
    profile: str,
) -> str | None:
    """Read the focused package without changing device or application state."""
    controller = _device_controller(
        adb_path,
        device,
        profile=profile,
        package_name=DEFAULT_PACKAGE_NAME,
    )
    for command in (("activity", "activities"), ("window", "windows")):
        try:
            output = controller.shell("dumpsys", *command, timeout=8)
        except AdbError:
            continue
        package = _parse_focused_package(output)
        if package:
            return package
    return None


def refresh_mumu_devices(
    adb_path: str | Path,
    current_devices: list[AndroidDevice],
) -> list[AndroidDevice]:
    """Always try the known MuMu TCP alias, then re-enumerate devices."""
    print(
        "[ADB] attempting MuMu TCP reconnect: "
        f"{MUMU_PRIMARY_TCP_ENDPOINT}"
    )
    connected = connect_endpoint(adb_path, MUMU_PRIMARY_TCP_ENDPOINT)
    if not connected:
        print("[ADB] MuMu TCP reconnect did not succeed; refreshing devices anyway")
    try:
        refreshed = list_devices(adb_path)
    except AdbError:
        if any(device.state == "device" for device in current_devices):
            print("[ADB] device refresh failed; keeping existing online candidates")
            return current_devices
        raise
    if (
        not connected
        and not any(device.state == "device" for device in refreshed)
        and any(device.state == "device" for device in current_devices)
    ):
        print("[ADB] reconnect failed; keeping existing online candidates")
        return current_devices
    return refreshed


def _matching_package_devices(
    adb_path: str | Path,
    devices: list[AndroidDevice],
    *,
    package_name: str,
    profile: str,
) -> list[AndroidDevice]:
    matches: list[AndroidDevice] = []
    for device in devices:
        print(f"[ADB] Checking {device.serial}")
        print(f"[ADB] model: {device.model or 'unknown'}")
        found = has_target_package(
            adb_path, device, package_name, profile=profile
        )
        print(f"[ADB] Miracle Nikki package: {'found' if found else 'not found'}")
        if found:
            matches.append(device)
    return matches


def select_device(
    adb_path: str | Path,
    devices: list[AndroidDevice],
    *,
    configured_serial: str | None,
    package_name: str,
    profile: str,
) -> AndroidDevice:
    usable = [device for device in devices if device.state == "device"]
    print(f"[ADB] adb devices: {len(usable)} usable devices")
    usable = deduplicate_device_aliases(
        adb_path,
        usable,
        configured_serial=configured_serial,
        profile=profile,
    )
    if len(usable) != sum(device.state == "device" for device in devices):
        print(f"[ADB] adb devices after alias merge: {len(usable)} unique devices")

    if configured_serial and len(usable) == 1:
        configured = next(
            (device for device in usable if device.serial == configured_serial), None
        )
        if configured is not None:
            if has_target_package(
                adb_path, configured, package_name, profile=profile
            ):
                print(f"[ADB] Configured device: {configured.serial}")
                print(f"[ADB] selected device: {configured.serial}")
                return configured
            print("[ADB] 配置设备未安装目标 package，进入自动检测")
        else:
            print("[ADB] 配置设备当前不可用，进入自动检测")

    matches = _matching_package_devices(
        adb_path,
        usable,
        package_name=package_name,
        profile=profile,
    )
    foreground_matches = [
        device
        for device in matches
        if query_foreground_package(adb_path, device, profile=profile)
        == package_name
    ]
    if len(foreground_matches) == 1:
        selected = foreground_matches[0]
        print(f"[ADB] foreground Miracle Nikki found: {selected.serial}")
        print(f"[ADB] selected device: {selected.serial}")
        return selected
    if len(foreground_matches) > 1:
        raise DeviceSelectionError(
            "multiple distinct Miracle Nikki instances are foreground; "
            "refusing random selection"
        )
    if configured_serial:
        configured = next(
            (device for device in matches if device.serial == configured_serial), None
        )
        if configured is not None:
            print(f"[ADB] selected device: {configured.serial} (configured)")
            return configured
    if len(matches) == 1:
        selected = matches[0]
        print(f"[ADB] selected device: {selected.serial}")
        return selected
    if len(matches) > 1:
        lines = ["检测到多个奇迹暖暖实例：", ""]
        for index, device in enumerate(matches, start=1):
            lines.extend(
                [
                    f"{index}. serial={device.serial}",
                    f"   model={device.model or 'unknown'}",
                    "",
                ]
            )
        lines.append("请配置 device_serial 指定目标实例。")
        raise DeviceSelectionError("\n".join(lines))
    if not usable:
        raise DeviceSelectionError("没有发现状态为 device 的 ADB 设备。")
    raise DeviceSelectionError(
        f"在线设备均未安装目标 package：{package_name}。"
    )


def _recover_device(
    adb_path: Path,
    original_serial: str,
    *,
    package_name: str,
    profile: str,
    strict_serial: bool = False,
) -> AndroidDevice | None:
    print(f"[ADB] Connection lost, checking original serial: {original_serial}")
    devices = list_devices(adb_path)
    if strict_serial:
        if is_tcp_serial(original_serial):
            connect_endpoint(adb_path, original_serial)
            devices = list_devices(adb_path)
        return next((device for device in devices
                     if device.serial == original_serial and device.state == "device"), None)
    if profile == "MuMu":
        devices = refresh_mumu_devices(adb_path, devices)
    original = next(
        (
            device
            for device in devices
            if device.serial == original_serial and device.state == "device"
        ),
        None,
    )
    if original is not None:
        print(f"[ADB] Recovered original serial: {original_serial}")
        return original
    usable = [device for device in devices if device.state == "device"]
    usable = deduplicate_device_aliases(
        adb_path,
        usable,
        configured_serial=original_serial,
        profile=profile,
    )
    matches = _matching_package_devices(
        adb_path, usable, package_name=package_name, profile=profile
    )
    if len(matches) == 1:
        print(f"[ADB] Rebound once: {matches[0].serial}")
        return matches[0]
    return None


def create_controller(
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    adb_path: str | Path | None = None,
    device_serial: str | None = None,
    package_name: str | None = None,
    profile: str | None = None,
    allow_fallback: bool = True,
    strict_serial: bool = False,
    auto_connect: bool = False,
) -> AdbController:
    config = load_connection_config(config_path)
    configured_serial = device_serial
    if configured_serial is None:
        configured_serial = config.get("device_serial") or config.get("serial")
    if configured_serial is not None and not isinstance(configured_serial, str):
        raise DeviceSelectionError("device_serial 必须为字符串或 null")

    selected_package = package_name or config.get("package_name") or DEFAULT_PACKAGE_NAME
    selected_profile = profile or config.get("profile") or "MuMu"
    if not isinstance(selected_package, str) or not selected_package:
        raise DeviceSelectionError("package_name 必须为非空字符串")
    if not isinstance(selected_profile, str) or not selected_profile:
        raise DeviceSelectionError("profile 必须为非空字符串")

    resolved_adb = resolve_adb_path(adb_path, config=config)
    # GUI batches explicitly target one address. Preserve legacy CLI discovery
    # unless strict_serial is requested; do not silently select another instance.
    if strict_serial and not configured_serial:
        raise DeviceSelectionError("请在设置中填写 ADB 地址或设备 serial。")
    if strict_serial:
        allow_fallback = False
    if auto_connect and configured_serial and is_tcp_serial(configured_serial):
        if not connect_endpoint(resolved_adb, configured_serial):
            raise DeviceSelectionError(f"ADB 自动连接失败：{configured_serial}")
    devices = list_devices(resolved_adb)
    if strict_serial:
        devices = [device for device in devices if device.serial == configured_serial]
        requested_device = next(iter(devices), None)
        if requested_device is None or requested_device.state != "device":
            state = requested_device.state if requested_device is not None else "未发现"
            raise DeviceSelectionError(
                f"ADB 设备 {configured_serial} 不可用（{state}）；"
                "请确认设备已启动并授权，或开启自动连接。"
            )
    if allow_fallback and selected_profile == "MuMu":
        devices = refresh_mumu_devices(resolved_adb, devices)
    usable = [device for device in devices if device.state == "device"]

    if not usable and allow_fallback and selected_profile == "MuMu":
        print("[ADB] No usable devices; entering MuMu fallback")
        for endpoint in discover_mumu_fallback_endpoints(configured_serial):
            if endpoint == MUMU_PRIMARY_TCP_ENDPOINT:
                continue
            if not connect_endpoint(resolved_adb, endpoint):
                continue
            devices = list_devices(resolved_adb)
            if any(device.state == "device" for device in devices):
                break

    selected = select_device(
        resolved_adb,
        devices,
        configured_serial=configured_serial,
        package_name=selected_package,
        profile=selected_profile,
    )
    controller = AdbController(
        adb_path=resolved_adb,
        serial=selected.serial,
        profile=selected_profile,
        model=selected.model,
        package_name=selected_package,
        recovery_callback=lambda original: _recover_device(
            resolved_adb,
            original,
            package_name=selected_package,
            profile=selected_profile,
            strict_serial=strict_serial,
        ),
    )
    _, android_version, model = controller.validate_connection()
    print(f"[ADB] serial = {controller.serial}")
    print(f"[ADB] model = {model or 'unknown'}")
    print(f"[ADB] Android = {android_version or 'unknown'}")
    print(f"[ADB] profile = {controller.profile.name}")
    print("[ADB] connection OK")
    return controller

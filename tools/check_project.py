from __future__ import annotations

import json
import argparse
import ast
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
INTERFACE = ROOT / "assets" / "interface.json"
RESOURCE = ROOT / "assets" / "resource"
PIPELINE = RESOURCE / "pipeline"
TEMPLATE_ROOT = RESOURCE / "image"
SCREENSHOT_ROOT = ROOT / "images" / "screenshots"
DEBUG_ROOT = ROOT / "images" / "debug"
OCR_MODEL_ROOT = RESOURCE / "model" / "ocr"
OCR_MODEL_FILES = ("det.onnx", "keys.txt", "rec.onnx")
MAIL_SAMPLE_FILES = {
    "daily_003.png": "真实系统邮件领取前页面",
    "daily_004.png": "真实奖励展示页面，OCR“跳过”离线验证通过",
    "daily_005.png": "真实奖励汇总页面，OCR“确定”离线验证通过",
    "daily_006.png": "真实系统邮件领取后页面",
}
ALLIANCE_SAMPLE_FILES = (
    "alliance_home.png",
    "alliance_commission_done.png",
    "alliance_commission_detail_done.png",
    "library_done.png",
)
REQUIRED_IMAGE_DIRS = (
    TEMPLATE_ROOT / "start",
    TEMPLATE_ROOT / "home",
    TEMPLATE_ROOT / "daily",
    TEMPLATE_ROOT / "common",
    TEMPLATE_ROOT / "alliance",
    TEMPLATE_ROOT / "competition",
    SCREENSHOT_ROOT / "start",
    SCREENSHOT_ROOT / "home",
    SCREENSHOT_ROOT / "daily",
    SCREENSHOT_ROOT / "alliance",
    SCREENSHOT_ROOT / "arena",
    SCREENSHOT_ROOT / "competition",
    DEBUG_ROOT / "current",
    DEBUG_ROOT / "unknown",
    DEBUG_ROOT / "pipeline",
)
EXPECTED_TEMPLATES = (
    "alliance/commission_back_arrow.png",
    "alliance/top_back_arrow.png",
    "arena/back_button.png",
    "common/chapter_back_arrow.png",
    "competition/home_back_button.png",
    "competition/judge_back_button.png",
    "competition/like_thumb.png",
    "daily/makeup_diamond.png",
    "daily/signin_claim.png",
    "daily/signin_entry.png",
    "daily/signin_page_marker.png",
    "decompose/workshop_back_arrow.png",
    "dreamweaver/missing_clothes_plus.png",
    "dreamweaver/purchase_plus_one.png",
    "home_pages/current_theme/left_arrow.png",
    "home_pages/current_theme/right_arrow.png",
    "home_random_visit/loading_snowflake_01.png",
    "home_random_visit/loading_snowflake_02.png",
    "home_random_visit/loading_snowflake_03.png",
    "start/enter_game_button.png",
    "start/start_button.png",
    "world_tour/event_collected_stamp.png",
)
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
REQUIRED_FILES = (
    ROOT / "agent" / "main.py",
    ROOT / "agent" / "adb_controller.py",
    ROOT / "agent" / "device_manager.py",
    ROOT / "agent" / "qjnn_action.py",
    ROOT / "agent" / "qjnn_alliance_commission.py",
    ROOT / "agent" / "qjnn_alliance_capture.py",
    ROOT / "agent" / "qjnn_alliance_daily.py",
    ROOT / "agent" / "qjnn_arena.py",
    ROOT / "agent" / "qjnn_arena_capture.py",
    ROOT / "agent" / "qjnn_competition.py",
    ROOT / "agent" / "qjnn_competition_capture.py",
    ROOT / "agent" / "qjnn_home.py",
    ROOT / "agent" / "qjnn_home_navigation.py",
    ROOT / "agent" / "qjnn_library.py",
    ROOT / "agent" / "qjnn_decompose.py",
    ROOT / "agent" / "qjnn_foreground.py",
    ROOT / "agent" / "qjnn_friend_send_heart.py",
    ROOT / "agent" / "qjnn_privacy.py",
    ROOT / "agent" / "qjnn_recognition.py",
    ROOT / "agent" / "qjnn_reward.py",
    ROOT / "agent" / "requirements.txt",
    ROOT / "tools" / "test_ocr.py",
    ROOT / "tools" / "test_startup_foreground.py",
    ROOT / "tools" / "test_custom_recognition_registration.py",
    ROOT / "tools" / "run_task.py",
    ROOT / "maatools.config.mts",
    ROOT / "package.json",
)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("根节点必须是 JSON object")
    return value


PIPELINE_REFERENCE_FIELDS = (
    "next",
    "on_success",
    "on_failure",
    "on_error",
    "candidate",
    "candidates",
    "reference",
    "references",
)


def check_pipeline_references(
    document: dict[str, Any],
    all_nodes: set[str],
    path: Path,
    errors: list[str],
) -> None:
    """Reject empty, anonymous, null, and missing Pipeline node targets."""

    def target_name(value: Any, location: str) -> str | None:
        if isinstance(value, str):
            name = value.strip()
            if not name:
                errors.append(f"{location}: 不允许空字符串节点引用")
                return None
            return name
        if isinstance(value, dict):
            name = value.get("name")
            if not isinstance(name, str) or not name.strip():
                errors.append(f"{location}: 不允许匿名或空对象节点 target")
                return None
            return name.strip()
        errors.append(f"{location}: 节点引用必须是非空字符串或带 name 的 object")
        return None

    def walk(value: Any, location: str) -> None:
        if isinstance(value, dict):
            for field, child in value.items():
                child_location = f"{location}.{field}"
                if field not in PIPELINE_REFERENCE_FIELDS:
                    walk(child, child_location)
                    continue
                references = child if isinstance(child, list) else [child]
                if not references:
                    errors.append(f"{child_location}: 不允许空候选节点数组")
                    continue
                for index, reference in enumerate(references):
                    name = target_name(reference, f"{child_location}[{index}]")
                    if name is not None and name not in all_nodes:
                        errors.append(
                            f"{child_location}[{index}]: 引用了不存在的节点 {name!r}"
                        )
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{location}[{index}]")

    for node_name, node in document.items():
        location = f"{path.relative_to(ROOT)}: 节点 {node_name!r}"
        if not isinstance(node_name, str) or not node_name.strip():
            errors.append(f"{path.relative_to(ROOT)}: 不允许匿名或空名称 Pipeline 节点")
            continue
        walk(node, location)


def check_pipeline_inverse_semantics(
    documents: list[tuple[Path, dict[str, Any]]],
    errors: list[str],
) -> None:
    """Reject inverse forms whose semantics Maa does not preserve in composites."""

    inverse_nodes = {
        name
        for _, document in documents
        for name, node in document.items()
        if isinstance(node, dict) and node.get("inverse") is True
    }

    def walk_inline(value: Any, location: str, inside_composite: bool = False) -> None:
        if isinstance(value, dict):
            if inside_composite and value.get("inverse") is True:
                errors.append(
                    f"{location}: 禁止在 And/Or 内联 recognition 中依赖 inverse 语义"
                )
            recognition = value.get("recognition")
            if recognition in {"And", "Or"}:
                for field in ("all_of", "any_of"):
                    candidates = value.get(field, [])
                    if not isinstance(candidates, list):
                        continue
                    for index, candidate in enumerate(candidates):
                        child_location = f"{location}.{field}[{index}]"
                        if isinstance(candidate, str) and candidate in inverse_nodes:
                            errors.append(
                                f"{child_location}: 复合 recognition 引用了顶层 inverse "
                                f"节点 {candidate!r}；必须让该节点由 Pipeline 顶层直接执行"
                            )
                        walk_inline(candidate, child_location, True)
            for field, child in value.items():
                if field not in {"all_of", "any_of"}:
                    walk_inline(child, f"{location}.{field}", inside_composite)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk_inline(child, f"{location}[{index}]", inside_composite)

    for path, document in documents:
        for node_name, node in document.items():
            walk_inline(
                node,
                f"{path.relative_to(ROOT)}: 节点 {node_name!r}",
            )


def check_python(errors: list[str]) -> None:
    for path in sorted((ROOT / "agent").glob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
            compile(source, str(path), "exec")
        except (OSError, SyntaxError, UnicodeError) as exc:
            errors.append(f"{path.relative_to(ROOT)}: Python 语法或编码错误：{exc}")
            continue

        if re.search(r"\bdevices\s*\[\s*0\s*\]", source):
            errors.append(f"{path.relative_to(ROOT)}: 禁止默认使用首个设备索引")
        if path.name != "device_manager.py" and re.search(r"127\.0\.0\.1:\d+", source):
            errors.append(f"{path.relative_to(ROOT)}: Python 源码中存在硬编码本地 ADB 端口")
        if path.name != "adb_controller.py" and re.search(r"\bsubprocess\b", source):
            errors.append(f"{path.relative_to(ROOT)}: subprocess 必须集中在 adb_controller.py")


def check_runtime_submission(runner_source: str, recovery_source: str, errors: list[str]) -> None:
    """Check the current submission wrapper without relying on old source spelling."""
    try:
        runner_tree = ast.parse(runner_source)
        recovery_tree = ast.parse(recovery_source)
    except SyntaxError as exc:
        errors.append(f"任务提交代码语法错误：{exc}")
        return
    pipeline = next((node for node in runner_tree.body
                     if isinstance(node, ast.FunctionDef) and node.name == "_run_pipeline"), None)
    wrapper = next((node for node in recovery_tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "run_with_recovery"), None)
    preflight_calls = []
    business_calls = []
    if pipeline is not None:
        for node in ast.walk(pipeline):
            if not isinstance(node, ast.Call):
                continue
            if (isinstance(node.func, ast.Attribute) and node.func.attr == "post_task"
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "QJNN_CommonPopupPreflight"):
                preflight_calls.append(node)
            if isinstance(node.func, ast.Name) and node.func.id == "run_with_recovery":
                business_calls.append(node)
    if not (len(preflight_calls) == len(business_calls) == 1
            and preflight_calls[0].lineno < business_calls[0].lineno):
        errors.append("run_task.py: common-popup preflight must run before the requested task")
    expected_args = ("tasker", "task_name", "runtime_override", "recovery_runtime")
    if not (len(business_calls) == 1 and len(business_calls[0].args) >= 4
            and all(isinstance(arg, ast.Name) and arg.id == expected
                    for arg, expected in zip(business_calls[0].args[:4], expected_args))):
        errors.append("run_task.py: 正式任务必须向恢复包装层传递任务与 runtime_override")
    submissions = [] if wrapper is None else [node for node in ast.walk(wrapper)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name) and node.func.value.id == "tasker"
        and node.func.attr == "post_task"]
    if not (len(submissions) == 1 and submissions[0].args
            and isinstance(submissions[0].args[0], ast.Name) and submissions[0].args[0].id == "entry"
            and any(keyword.arg == "pipeline_override" and isinstance(keyword.value, ast.Name)
                    and keyword.value.id == "override" for keyword in submissions[0].keywords)):
        errors.append("qjnn_back_to_home.py: 正式任务提交必须保留 pipeline_override")


def missing_input(path: Path, errors: list[str], message: str, source_only: bool) -> None:
    optional = (path.is_relative_to(SCREENSHOT_ROOT) or path.is_relative_to(DEBUG_ROOT)
                or path == TEMPLATE_ROOT / "home")
    if source_only and optional:
        print(f"[SKIP] Optional local input absent: {path.relative_to(ROOT)}")
    else:
        errors.append(message)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Static project checks without device access")
    parser.add_argument("--without-private-samples", action="store_true",
                        help="Report missing private samples and empty local-only folders as skipped")
    source_only = parser.parse_args(argv).without_private_samples
    errors: list[str] = []
    missing_templates: set[str] = set()
    invalid_alliance_samples: list[str] = []
    nodes: dict[str, Path] = {}
    pipeline_documents: list[tuple[Path, dict[str, Any]]] = []

    for path in REQUIRED_FILES:
        if not path.is_file():
            # Exported source copies may omit private-sample regression tools.
            # Full development checks still require every listed test/tool.
            if source_only and path.parent == ROOT / "tools" and path.name.startswith("test_"):
                print(f"[SKIP] Optional development test absent: {path.relative_to(ROOT)}")
            else:
                errors.append(f"缺少文件：{path.relative_to(ROOT)}")

    try:
        interface = load_json(INTERFACE)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"[ERROR] {INTERFACE}: {exc}")
        return 1

    try:
        load_json(RESOURCE / "default_pipeline.json")
        load_json(ROOT / "package.json")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"JSON 配置无效：{exc}")

    for path in sorted(PIPELINE.rglob("*.json")):
        try:
            document = load_json(path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"{path.relative_to(ROOT)}: {exc}")
            continue
        pipeline_documents.append((path, document))

        for name, node in document.items():
            if name in nodes:
                errors.append(
                    f"重复节点 {name!r}: {nodes[name].relative_to(ROOT)} 和 "
                    f"{path.relative_to(ROOT)}"
                )
            nodes[name] = path
            if not isinstance(node, dict):
                errors.append(f"{path.relative_to(ROOT)}: 节点 {name!r} 必须是 object")

            templates = node.get("template", []) if isinstance(node, dict) else []
            if isinstance(templates, str):
                templates = [templates]
            for template in templates:
                if not (TEMPLATE_ROOT / template).is_file():
                    missing_templates.add(str(template))

    check_pipeline_inverse_semantics(pipeline_documents, errors)

    tasks = interface.get("task", [])
    if not isinstance(tasks, list):
        errors.append("interface.json: task 必须是数组")
    else:
        for task in tasks:
            entry = task.get("entry") if isinstance(task, dict) else None
            if entry not in nodes:
                errors.append(f"interface.json: 任务入口 {entry!r} 不存在")

        end_to_end_tasks = [
            task
            for task in tasks
            if isinstance(task, dict) and task.get("entry") == "QJNN_StartAndSignin"
        ]
        if len(end_to_end_tasks) != 1:
            errors.append("interface.json: 必须且只能有一个 QJNN_StartAndSignin 正式任务入口")
        elif end_to_end_tasks[0].get("name") != "启动并签到":
            errors.append("interface.json: QJNN_StartAndSignin 的显示名称必须为“启动并签到”")

        reward_debug_tasks = [
            task
            for task in tasks
            if isinstance(task, dict) and task.get("entry") == "QJNN_SigninRewardDebug"
        ]
        if len(reward_debug_tasks) != 1:
            errors.append("interface.json: 必须且只能有一个 QJNN_SigninRewardDebug 调试任务入口")
        elif reward_debug_tasks[0].get("name") != "签到奖励流调试":
            errors.append(
                "interface.json: QJNN_SigninRewardDebug 的显示名称必须为“签到奖励流调试”"
            )

        mail_tasks = [
            task
            for task in tasks
            if isinstance(task, dict) and task.get("entry") == "QJNN_Mail"
        ]
        if len(mail_tasks) != 1:
            errors.append("interface.json: 必须且只能有一个 QJNN_Mail 正式任务入口")
        elif mail_tasks[0].get("name") != "系统邮件领取":
            errors.append("interface.json: QJNN_Mail 的显示名称必须为“系统邮件领取”")

        home_to_p3_tasks = [
            task
            for task in tasks
            if isinstance(task, dict) and task.get("entry") == "QJNN_HomeToP3"
        ]
        if len(home_to_p3_tasks) != 1:
            errors.append("interface.json: 必须且只能有一个 QJNN_HomeToP3 任务入口")
        elif home_to_p3_tasks[0].get("name") != "主页导航到 P3":
            errors.append("interface.json: QJNN_HomeToP3 的显示名称必须为“主页导航到 P3”")

        alliance_commission_tasks = [
            task
            for task in tasks
            if isinstance(task, dict) and task.get("entry") == "QJNN_AllianceCommission"
        ]
        if len(alliance_commission_tasks) != 1:
            errors.append("interface.json: 必须且只能有一个 QJNN_AllianceCommission 任务入口")
        elif alliance_commission_tasks[0].get("name") != "搭配师联盟委托":
            errors.append(
                "interface.json: QJNN_AllianceCommission 的显示名称必须为“搭配师联盟委托”"
            )

        settlement_debug_tasks = [
            task
            for task in tasks
            if isinstance(task, dict)
            and task.get("entry") == "QJNN_AllianceCommissionSettlementDebug"
        ]
        if len(settlement_debug_tasks) != 1:
            errors.append(
                "interface.json: 必须且只能有一个 "
                "QJNN_AllianceCommissionSettlementDebug 调试任务入口"
            )
        elif settlement_debug_tasks[0].get("name") != "联盟委托结算调试":
            errors.append(
                "interface.json: QJNN_AllianceCommissionSettlementDebug "
                "的显示名称必须为“联盟委托结算调试”"
            )

        after_commission_debug_tasks = [
            task
            for task in tasks
            if isinstance(task, dict)
            and task.get("entry") == "QJNN_AllianceAfterCommissionDebug"
        ]
        if len(after_commission_debug_tasks) != 1:
            errors.append(
                "interface.json: 必须且只能有一个 "
                "QJNN_AllianceAfterCommissionDebug 调试任务入口"
            )
        elif after_commission_debug_tasks[0].get("name") != "联盟委托后半段续跑调试":
            errors.append(
                "interface.json: QJNN_AllianceAfterCommissionDebug "
                "的显示名称必须为“联盟委托后半段续跑调试”"
            )

        library_tasks = [
            task
            for task in tasks
            if isinstance(task, dict) and task.get("entry") == "QJNN_Library"
        ]
        if len(library_tasks) != 1:
            errors.append("interface.json: 必须且只能有一个 QJNN_Library 任务入口")
        elif library_tasks[0].get("name") != "搭配师联盟图书馆":
            errors.append(
                "interface.json: QJNN_Library 的显示名称必须为“搭配师联盟图书馆”"
            )

        alliance_daily_tasks = [
            task
            for task in tasks
            if isinstance(task, dict) and task.get("entry") == "QJNN_AllianceDaily"
        ]
        if len(alliance_daily_tasks) != 1:
            errors.append("interface.json: 必须且只能有一个 QJNN_AllianceDaily 任务入口")
        elif alliance_daily_tasks[0].get("name") != "搭配师联盟日常":
            errors.append(
                "interface.json: QJNN_AllianceDaily 的显示名称必须为“搭配师联盟日常”"
            )

        arena_daily_tasks = [
            task
            for task in tasks
            if isinstance(task, dict) and task.get("entry") == "QJNN_ArenaDaily"
        ]
        if len(arena_daily_tasks) != 1:
            errors.append("interface.json: 必须且只能有一个 QJNN_ArenaDaily 任务入口")
        elif arena_daily_tasks[0].get("name") != "搭配竞技场日常":
            errors.append(
                "interface.json: QJNN_ArenaDaily 的显示名称必须为“搭配竞技场日常”"
            )

        arena_exit_debug_tasks = [
            task
            for task in tasks
            if isinstance(task, dict) and task.get("entry") == "QJNN_ArenaExitDebug"
        ]
        if len(arena_exit_debug_tasks) != 1:
            errors.append("interface.json: 必须且只能有一个 QJNN_ArenaExitDebug 调试入口")
        elif arena_exit_debug_tasks[0].get("name") != "竞技场退出调试":
            errors.append(
                "interface.json: QJNN_ArenaExitDebug 的显示名称必须为“竞技场退出调试”"
            )

        competition_daily_tasks = [
            task
            for task in tasks
            if isinstance(task, dict) and task.get("entry") == "QJNN_CompetitionDaily"
        ]
        if len(competition_daily_tasks) != 1:
            errors.append("interface.json: 必须且只能有一个 QJNN_CompetitionDaily 任务入口")
        elif competition_daily_tasks[0].get("name") != "搭配评选赛日常":
            errors.append(
                "interface.json: QJNN_CompetitionDaily 的显示名称必须为“搭配评选赛日常”"
            )

        competition_exit_debug_tasks = [
            task
            for task in tasks
            if isinstance(task, dict)
            and task.get("entry") == "QJNN_CompetitionExitDebug"
        ]
        if len(competition_exit_debug_tasks) != 1:
            errors.append(
                "interface.json: 必须且只能有一个 QJNN_CompetitionExitDebug 调试入口"
            )

        common_popup_debug_tasks = [
            task
            for task in tasks
            if isinstance(task, dict) and task.get("entry") == "QJNN_CommonPopupDebug"
        ]
        if len(common_popup_debug_tasks) != 1:
            errors.append(
                "interface.json: 必须且只能有一个 QJNN_CommonPopupDebug 调试入口"
            )

    for directory in REQUIRED_IMAGE_DIRS:
        if not directory.is_dir():
            missing_input(directory, errors, f"缺少图片目录：{directory.relative_to(ROOT)}", source_only)

    mail_sample_root = SCREENSHOT_ROOT / "daily"
    for filename in MAIL_SAMPLE_FILES:
        sample = mail_sample_root / filename
        if not sample.is_file():
            missing_input(sample, errors, f"缺少已登记的邮箱离线样本：{sample.relative_to(ROOT)}", source_only)

    alliance_sample_root = SCREENSHOT_ROOT / "alliance"
    for filename in ALLIANCE_SAMPLE_FILES:
        sample = alliance_sample_root / filename
        if not sample.is_file():
            missing_input(sample, errors, f"缺少已登记的联盟离线样本：{sample.relative_to(ROOT)}", source_only)
    settlement_sample = alliance_sample_root / "alliance_commission_settlement_real_002.png"
    if settlement_sample.is_file():
        if settlement_sample.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
            invalid_alliance_samples.append(
                "alliance_commission_settlement_real_002.png（PNG 二进制签名无效）"
            )
    else:
        invalid_alliance_samples.append("alliance_commission_settlement_real_002.png（缺失）")

    for template in EXPECTED_TEMPLATES:
        if not (TEMPLATE_ROOT / template).is_file():
            missing_templates.add(template)

    for directory in (TEMPLATE_ROOT, SCREENSHOT_ROOT, DEBUG_ROOT):
        if directory.is_dir():
            for path in directory.iterdir():
                if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                    errors.append(f"图片不应直接放在分类根目录：{path.relative_to(ROOT)}")

    main_source = (ROOT / "agent" / "main.py").read_text(encoding="utf-8")
    recognition_source = (ROOT / "agent" / "qjnn_recognition.py").read_text(encoding="utf-8")
    startup_path = PIPELINE / "10_startup.json"
    try:
        startup = load_json(startup_path)
        check_pipeline_references(startup, set(nodes), startup_path, errors)
        startup_entry = startup.get("QJNN_StartAndEnterHome", {})
        if not isinstance(startup_entry, dict) or startup_entry.get("max_hit") != 1:
            errors.append(
                "10_startup.json: QJNN_StartAndEnterHome.max_hit 必须为 1，"
                "供 jump_back 串联后继续签到"
            )
        expected_startup_router = [
            "QJNN_PrivateLoginForeground",
            "QJNN_UpdateConfirm",
            "QJNN_InGamePage",
            "QJNN_HomeScreenTemplateGate",
            "QJNN_StartButtonTemplateGate",
            "QJNN_EnterGameScreenTemplateGate",
            "QJNN_QjnnForeground",
            "QJNN_StartAppAndEnterHome",
        ]
        if not isinstance(startup_entry, dict) or startup_entry.get("next") != expected_startup_router:
            errors.append(
                "10_startup.json: QJNN_StartAndEnterHome 必须先识别现有页面和前台包，"
                "最后才允许 StartApp"
            )

        foreground = startup.get("QJNN_QjnnForeground", {})
        expected_foreground_fields = {
            "recognition": "Custom",
            "custom_recognition": "qjnn_package_foreground",
            "custom_recognition_param": {"package": "com.tencent.tmgp.qjnn"},
            "action": "DoNothing",
            "next": ["QJNN_WaitForGameState"],
        }
        if not isinstance(foreground, dict):
            errors.append("10_startup.json: 缺少 QJNN_QjnnForeground")
        else:
            for field, expected in expected_foreground_fields.items():
                if foreground.get(field) != expected:
                    errors.append(
                        f"10_startup.json: QJNN_QjnnForeground.{field} 必须为 {expected!r}"
                    )

        wait_for_state = startup.get("QJNN_WaitForGameState", {})
        expected_wait_next = [
            "QJNN_PrivateLoginForeground",
            "QJNN_UpdateConfirm",
            "QJNN_LoginScreen",
            "QJNN_InGamePage",
            "QJNN_HomeScreenTemplateGate",
            "QJNN_HomeScreen",
            "QJNN_StartButton",
            "QJNN_EnterGameScreen",
        ]
        if not isinstance(wait_for_state, dict):
            errors.append("10_startup.json: 缺少 QJNN_WaitForGameState")
        elif wait_for_state.get("action") != "DoNothing":
            errors.append("10_startup.json: QJNN_WaitForGameState 只能等待，不得输入")
        elif wait_for_state.get("next") != expected_wait_next:
            errors.append("10_startup.json: QJNN_WaitForGameState 等待状态列表不完整")
        elif wait_for_state.get("timeout") != 120000:
            errors.append("10_startup.json: QJNN_WaitForGameState.timeout 必须为 120000")

        start_app_and_wait = startup.get("QJNN_StartAppAndEnterHome", {})
        if not isinstance(start_app_and_wait, dict):
            errors.append("10_startup.json: 缺少 QJNN_StartAppAndEnterHome")
        elif start_app_and_wait.get("action") != "StartApp":
            errors.append("10_startup.json: QJNN_StartAppAndEnterHome 必须沿用 StartApp")
        elif start_app_and_wait.get("max_hit") != 1:
            errors.append("10_startup.json: QJNN_StartAppAndEnterHome.max_hit 必须为 1")
        elif start_app_and_wait.get("next") != ["QJNN_WaitForGameState"]:
            errors.append("10_startup.json: StartApp 后必须进入明确命名的等待状态")
        elif start_app_and_wait.get("timeout") != 120000:
            errors.append("10_startup.json: StartApp 后等待 timeout 必须为 120000")
        start_button = startup.get("QJNN_StartButton", {})
        if not isinstance(start_button, dict):
            errors.append("10_startup.json: QJNN_StartButton 必须是 object")
        else:
            expected_start_fields = {
                "recognition": "Or",
                "any_of": [
                    {
                        "recognition": "OCR",
                        "expected": ["点击开始"],
                        "threshold": 0.3,
                    },
                    {
                        "recognition": "TemplateMatch",
                        "template": "start/start_button.png",
                        "threshold": 0.85,
                    },
                ],
                "action": "Click",
                "target": True,
            }
            for field, expected in expected_start_fields.items():
                if start_button.get(field) != expected:
                    errors.append(
                        f"10_startup.json: QJNN_StartButton.{field} 必须为 {expected!r}"
                    )
            next_nodes = start_button.get("next", [])
            expected_start_next = ["QJNN_WaitForGameState"]
            if next_nodes != expected_start_next:
                errors.append(
                    "10_startup.json: QJNN_StartButton 点击后必须返回游戏状态等待"
                )
            if "timeout" not in start_button or "on_error" not in start_button:
                errors.append(
                    "10_startup.json: QJNN_StartButton 必须由 Pipeline 管理 timeout/on_error"
                )

        enter_game = startup.get("QJNN_EnterGameScreen", {})
        if not isinstance(enter_game, dict):
            errors.append("10_startup.json: QJNN_EnterGameScreen 必须是 object")
        else:
            expected_enter_fields = {
                "recognition": "Or",
                "any_of": [
                    {
                        "recognition": "OCR",
                        "expected": ["进入游戏"],
                        "threshold": 0.3,
                    },
                    {
                        "recognition": "TemplateMatch",
                        "template": "start/enter_game_button.png",
                        "threshold": 0.85,
                    },
                ],
                "action": "Click",
                "target": True,
                "rate_limit": 1500,
                "timeout": 20000,
                "post_delay": 800,
                "next": ["QJNN_WaitForGameState"],
                "on_error": ["QJNN_SafeStop"],
            }
            for field, expected in expected_enter_fields.items():
                if enter_game.get(field) != expected:
                    errors.append(
                        f"10_startup.json: QJNN_EnterGameScreen.{field} 必须为 {expected!r}"
                    )
            enter_template = TEMPLATE_ROOT / "start" / "enter_game_button.png"
            if not enter_template.is_file():
                errors.append(
                    "缺少已启用的进入游戏按钮模板："
                    "assets/resource/image/start/enter_game_button.png"
                )

        login_screen = startup.get("QJNN_LoginScreen", {})
        if not isinstance(login_screen, dict):
            errors.append("10_startup.json: QJNN_LoginScreen 必须是 object")
        elif (login_screen.get("recognition") != "Custom"
              or login_screen.get("custom_recognition") != "qjnn_login_screen"):
            errors.append("Login screen must use provider-neutral OCR recognition")
        elif login_screen.get("action") != "DoNothing":
            errors.append("10_startup.json: QJNN_LoginScreen 禁止执行登录点击")
        elif login_screen.get("next") != ["QJNN_PrivateLoginWait"]:
            errors.append("10_startup.json: QJNN_LoginScreen 必须进入第三方登录隐私等待")

        private_login_wait = startup.get("QJNN_PrivateLoginWait", {})
        expected_private_wait = {
            "recognition": "DirectHit",
            "action": "Custom",
            "custom_action": "qjnn_private_login_wait",
            "custom_action_param": {
                "timeout_ms": 300000,
                "interval_ms": 1000,
            },
            "next": [
                "QJNN_StartButton",
                "QJNN_EnterGameScreen",
                "QJNN_HomeScreen",
            ],
            "on_error": ["QJNN_PrivateLoginTimeout"],
        }
        if not isinstance(private_login_wait, dict):
            errors.append("10_startup.json: 缺少 QJNN_PrivateLoginWait")
        else:
            for field, expected in expected_private_wait.items():
                if private_login_wait.get(field) != expected:
                    errors.append(
                        f"10_startup.json: QJNN_PrivateLoginWait.{field} 必须为 {expected!r}"
                    )

        private_login_timeout = startup.get("QJNN_PrivateLoginTimeout", {})
        if not isinstance(private_login_timeout, dict):
            errors.append("10_startup.json: 缺少 QJNN_PrivateLoginTimeout")
        elif private_login_timeout.get("recognition") != "DirectHit":
            errors.append("10_startup.json: 隐私等待超时节点不得执行 OCR")
        elif private_login_timeout.get("next") != ["QJNN_SafeStop"]:
            errors.append("10_startup.json: 隐私等待超时后必须安全停止")

        private_login_foreground = startup.get("QJNN_PrivateLoginForeground", {})
        expected_private_foreground = {
            "recognition": "Custom",
            "custom_recognition": "qjnn_private_login_foreground",
            "action": "DoNothing",
            "next": ["QJNN_PrivateLoginWait"],
        }
        if not isinstance(private_login_foreground, dict):
            errors.append("10_startup.json: 缺少第三方登录前台检测节点")
        else:
            for field, expected in expected_private_foreground.items():
                if private_login_foreground.get(field) != expected:
                    errors.append(
                        f"10_startup.json: QJNN_PrivateLoginForeground.{field} "
                        f"必须为 {expected!r}"
                    )

        login_template_gate = startup.get("QJNN_LoginScreenGate", {})
        expected_login_template_gate = {
            "recognition": "Custom",
            "custom_recognition": "qjnn_login_screen",
            "action": "DoNothing",
            "next": ["QJNN_LoginScreen"],
        }
        if not isinstance(login_template_gate, dict):
            errors.append("10_startup.json: 缺少登录页隐私模板门")
        else:
            for field, expected in expected_login_template_gate.items():
                if login_template_gate.get(field) != expected:
                    errors.append(
                        f"10_startup.json: QJNN_LoginScreenGate.{field} "
                        f"必须为 {expected!r}"
                    )

        expected_safe_gates = {
            "QJNN_StartButtonTemplateGate": (
                "start/start_button.png",
                0.6,
                "QJNN_StartButton",
            ),
            "QJNN_EnterGameScreenTemplateGate": (
                "start/enter_game_button.png",
                0.85,
                "QJNN_EnterGameScreen",
            ),
        }
        for node_name, (template, threshold, target) in expected_safe_gates.items():
            node = startup.get(node_name, {})
            expected_fields = {
                "recognition": "TemplateMatch",
                "template": template,
                "threshold": threshold,
                "action": "DoNothing",
                "next": [target],
            }
            if not isinstance(node, dict):
                errors.append(f"10_startup.json: 缺少安全模板门 {node_name}")
                continue
            for field, expected in expected_fields.items():
                if node.get(field) != expected:
                    errors.append(
                        f"10_startup.json: {node_name}.{field} 必须为 {expected!r}"
                    )

        home_screen = startup.get("QJNN_HomeScreen", {})
        expected_home_recognition = [
            {
                "recognition": "OCR",
                "expected": ["任务"],
                "threshold": 0.3,
            },
            {
                "recognition": "OCR",
                "expected": ["好友"],
                "threshold": 0.3,
            },
        ]
        if not isinstance(home_screen, dict):
            errors.append("10_startup.json: QJNN_HomeScreen 必须是 object")
        elif home_screen.get("recognition") != "And":
            errors.append("10_startup.json: QJNN_HomeScreen 必须使用 And 组合识别")
        elif home_screen.get("all_of") != expected_home_recognition:
            errors.append("10_startup.json: QJNN_HomeScreen 必须同时 OCR 识别“任务”和“好友”")
        elif home_screen.get("action") != "DoNothing":
            errors.append("10_startup.json: QJNN_HomeScreen 只确认页面，不应执行点击")
        elif "template" in home_screen:
            errors.append("10_startup.json: QJNN_HomeScreen 不应依赖 home_marker.png")
        else:
            expected_home_handoff = {
                "rate_limit": 1000,
                "timeout": 10000,
                "next": ["QJNN_HomeStable"],
                "on_error": ["QJNN_HomeStableTimeout"],
            }
            for field, expected in expected_home_handoff.items():
                if home_screen.get(field) != expected:
                    errors.append(
                        f"10_startup.json: QJNN_HomeScreen.{field} 必须为 {expected!r}"
                    )

        home_stable = startup.get("QJNN_HomeStable", {})
        home_gate=startup.get('QJNN_HomeScreenTemplateGate',{})
        if any(home_gate.get(k)!=v for k,v in {
                'recognition':'Custom','custom_recognition':'qjnn_home_any_page',
                'custom_recognition_param':{'strict':True},'action':'DoNothing',
                'post_delay':400,'next':['QJNN_HomeStable']}.items()):
            errors.append('10_startup.json: HOME gate must confirm a real page before the second frame')
        expected_home_stable = {
            "recognition": "Custom",
            "custom_recognition": "qjnn_home_any_page",
            "custom_recognition_param": {"strict": True},
            "action": "DoNothing",
        }
        if not isinstance(home_stable, dict):
            errors.append("10_startup.json: 缺少 QJNN_HomeStable")
        else:
            for field, expected in expected_home_stable.items():
                if home_stable.get(field) != expected:
                    errors.append(
                        f"10_startup.json: QJNN_HomeStable.{field} 必须为 {expected!r}"
                    )

        home_stable_timeout = startup.get("QJNN_HomeStableTimeout", {})
        expected_home_stable_timeout = {
            "recognition": "DirectHit",
            "action": "DoNothing",
            "next": ["QJNN_SafeStop"],
            "focus": {
                "Node.Action.Starting": (
                    "[Startup] Home base UI detected, but async home controls "
                    "did not become ready"
                )
            },
        }
        if not isinstance(home_stable_timeout, dict):
            errors.append("10_startup.json: 缺少 QJNN_HomeStableTimeout")
        else:
            for field, expected in expected_home_stable_timeout.items():
                if home_stable_timeout.get(field) != expected:
                    errors.append(
                        "10_startup.json: "
                        f"QJNN_HomeStableTimeout.{field} 必须为 {expected!r}"
                    )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"{startup_path.relative_to(ROOT)}: {exc}")

    signin_path = PIPELINE / "30_signin.json"
    try:
        signin = load_json(signin_path)
        required_signin_templates = {
            "daily/signin_entry.png",
            "daily/signin_page_marker.png",
            "daily/signin_claim.png",
        }
        for template in sorted(required_signin_templates):
            if not (TEMPLATE_ROOT / template).is_file():
                errors.append(f"缺少签到模板：assets/resource/image/{template}")

        def ocr_or_template(text: str, template: str, threshold: float) -> list[dict[str, Any]]:
            return [
                {
                    "recognition": "OCR",
                    "expected": [text],
                    "threshold": 0.3,
                },
                {
                    "recognition": "TemplateMatch",
                    "template": template,
                    "threshold": threshold,
                },
            ]

        signin_screen_recognition = ocr_or_template(
            "暖暖月签到", "daily/signin_page_marker.png", 0.85
        )
        signin_claim_recognition = ocr_or_template(
            "点击领取", "daily/signin_claim.png", 0.85
        )

        expected_signin_nodes = {
            "QJNN_SigninEntryText": {
                "recognition": "OCR",
                "expected": ["签到"],
                "threshold": 0.3,
            },
            "QJNN_SigninPendingBadge": {
                "recognition": "ColorMatch",
                "lower": [[210, 20, 20]],
                "upper": [[255, 130, 130]],
                "method": 4,
                "count": 50,
                "connected": True,
                "roi": "QJNN_SigninEntryText",
                "roi_offset": [37, -94, 14, 41],
            },
            "QJNN_SigninEntry": {
                "recognition": "Or",
                "any_of": [
                    {
                        "recognition": "And",
                        "all_of": [
                            "QJNN_SigninEntryText",
                            "QJNN_SigninPendingBadge",
                        ],
                        "box_index": 0,
                    },
                    {
                        "recognition": "TemplateMatch",
                        "template": "daily/signin_entry.png",
                        "threshold": 0.95,
                    },
                ],
                "action": "Click",
                "target": True,
                "next": [
                    "QJNN_SigninScreenAfterClaim",
                    "QJNN_SigninScreen",
                ],
            },
            "QJNN_SigninScreen": {
                "recognition": "Custom",
                "custom_recognition": "qjnn_signin_state",
                "custom_recognition_param": {"state": "CLAIMABLE"},
                "next": ["QJNN_SigninClaim"],
            },
            "QJNN_SigninPageContext": {
                "recognition": "Or",
                "any_of": signin_screen_recognition,
                "action": "DoNothing",
            },
            "QJNN_SigninClaim": {
                "recognition": "Or",
                "any_of": signin_claim_recognition,
                "action": "Click",
                "target": True,
                "next": ["QJNN_SigninRewardFlow"],
            },
            "QJNN_SigninClaimGone": {
                "recognition": "Or",
                "any_of": signin_claim_recognition,
                "inverse": True,
                "next": ["QJNN_SigninClose"],
            },
            "QJNN_HomeAfterSignin": {
                "recognition": "Or",
                "any_of": signin_screen_recognition,
                "inverse": True,
                "next": ["QJNN_SigninCompletedOnHome"],
            },
        }
        for node_name, expected_fields in expected_signin_nodes.items():
            node = signin.get(node_name)
            if not isinstance(node, dict):
                errors.append(f"30_signin.json: 缺少签到节点 {node_name}")
                continue
            for field, expected in expected_fields.items():
                if node.get(field) != expected:
                    errors.append(
                        f"30_signin.json: {node_name}.{field} 必须为 {expected!r}"
                    )

        expected_rect_nodes = {
            "QJNN_SigninRewardDismiss": [240, 160, 240, 64],
            "QJNN_SigninClose": [240, 220, 240, 48],
        }
        for node_name, target in expected_rect_nodes.items():
            node = signin.get(node_name)
            if not isinstance(node, dict):
                errors.append(f"30_signin.json: 缺少签到节点 {node_name}")
            elif node.get("recognition") != "DirectHit" or node.get("action") != "Click":
                errors.append(f"30_signin.json: {node_name} 必须使用原生 DirectHit + Click")
            elif node.get("target") != target:
                errors.append(f"30_signin.json: {node_name}.target 必须为矩形 {target!r}")

        reward_card_rect = [103, 300, 512, 680]
        reward_safe_rect = [205, 436, 308, 408]
        share_forbidden_rect = [480, 960, 220, 140]

        def rect_contains(outer: list[int], inner: list[int]) -> bool:
            ox, oy, ow, oh = outer
            ix, iy, iw, ih = inner
            return (
                ix >= ox
                and iy >= oy
                and ix + iw <= ox + ow
                and iy + ih <= oy + oh
            )

        def rects_intersect(first: list[int], second: list[int]) -> bool:
            ax, ay, aw, ah = first
            bx, by, bw, bh = second
            return not (
                ax + aw <= bx
                or bx + bw <= ax
                or ay + ah <= by
                or by + bh <= ay
            )

        reward_flow = signin.get("QJNN_SigninRewardFlow", {})
        reward_debug = signin.get("QJNN_SigninRewardDebug", {})
        expected_reward_debug = {
            "recognition": "DirectHit",
            "action": "DoNothing",
            "next": ["QJNN_SigninRewardFlow"],
            "on_error": ["QJNN_SigninFailure"],
        }
        if not isinstance(reward_debug, dict):
            errors.append("30_signin.json: 缺少 QJNN_SigninRewardDebug")
        else:
            for field, expected in expected_reward_debug.items():
                if reward_debug.get(field) != expected:
                    errors.append(
                        "30_signin.json: QJNN_SigninRewardDebug."
                        f"{field} 必须为 {expected!r}"
                    )
            debug_focus = reward_debug.get("focus", {})
            if not isinstance(debug_focus, dict) or debug_focus.get(
                "Node.Action.Starting"
            ) != "[SigninRewardDebug] Starting from current screen":
                errors.append("30_signin.json: RewardDebug 缺少规定的开始日志")

        expected_reward_candidates = [
            "QJNN_SigninRewardPopup",
            "QJNN_RewardSuitShowcase",
            "QJNN_RewardClothesScreen",
            "QJNN_SigninScreenAfterClaim",
            "QJNN_SigninRewardHome",
        ]
        if not isinstance(reward_flow, dict):
            errors.append("30_signin.json: 缺少 QJNN_SigninRewardFlow")
        else:
            if reward_flow.get("action") != "DoNothing":
                errors.append("30_signin.json: RewardFlow 本身不得点击")
            if reward_flow.get("next") != expected_reward_candidates:
                errors.append("30_signin.json: RewardFlow 必须只包含明确奖励/完成状态")
            if reward_flow.get("on_error") != ["QJNN_SigninFailure"]:
                errors.append("30_signin.json: RewardFlow 未知状态必须进入 SigninFailure")
            timeout = reward_flow.get("timeout")
            max_hit = reward_flow.get("max_hit")
            if not isinstance(timeout, int) or not 1000 <= timeout <= 30000:
                errors.append("30_signin.json: RewardFlow 必须配置 1~30 秒有限 timeout")
            if max_hit != 13:
                errors.append(
                    "30_signin.json: RewardFlow.max_hit 必须为 13，"
                    "将总 reward dismiss 限制在 13 次以内"
                )
            for candidate_name in expected_reward_candidates:
                candidate = signin.get(candidate_name, {})
                if isinstance(candidate, dict) and candidate.get("recognition") == "DirectHit":
                    errors.append(
                        f"30_signin.json: RewardFlow 未知状态禁止由 {candidate_name} DirectHit"
                    )

        reward_expected = ["恭喜获得", "获得奖励"]
        reward_roi = [160, 80, 400, 560]

        reward_home = signin.get("QJNN_SigninRewardHome", {})
        expected_reward_home = {
            "recognition": "And",
            "all_of": ["QJNN_HomeScreen"],
            "action": "DoNothing",
            "next": ["QJNN_SigninCompletedOnHome"],
        }
        if not isinstance(reward_home, dict) or any(
            reward_home.get(field) != expected
            for field, expected in expected_reward_home.items()
        ):
            errors.append(
                "30_signin.json: SigninRewardHome 必须只做 HOME 正向识别并进入统一完成链"
            )

        screen_after_claim = signin.get("QJNN_SigninScreenAfterClaim", {})
        expected_claimed_screen = {
            "recognition": "Custom",
            "custom_recognition": "qjnn_signin_state",
            "custom_recognition_param": {"state": "CLAIMED"},
            "action": "DoNothing",
            "post_delay": 800,
            "next": ["QJNN_SigninScreen", "QJNN_SigninClaimGone"],
        }
        if not isinstance(screen_after_claim, dict):
            errors.append("30_signin.json: 缺少 SigninScreenAfterClaim")
        elif any(
            screen_after_claim.get(field) != expected
            for field, expected in expected_claimed_screen.items()
        ):
            errors.append(
                "30_signin.json: SigninScreenAfterClaim 必须使用 CLAIMED 互斥状态识别"
            )

        completed_home = signin.get("QJNN_SigninCompletedOnHome", {})
        expected_completed_home = {
            "recognition": "And",
            "all_of": [{"recognition":"Custom", "custom_recognition":"qjnn_home_any_page", "custom_recognition_param":{"strict":True}}, "QJNN_SigninEntryText"],
            "action": "DoNothing",
            "next": ["QJNN_SigninCompletedRewardAbsent"],
        }
        if not isinstance(completed_home, dict) or any(
            completed_home.get(field) != expected
            for field, expected in expected_completed_home.items()
        ):
            errors.append(
                "30_signin.json: SigninCompletedOnHome 必须确认 HOME 和签到入口上下文"
            )

        completed_reward_absent = signin.get(
            "QJNN_SigninCompletedRewardAbsent", {}
        )
        expected_completed_reward_absent = {
            "recognition": "OCR",
            "expected": reward_expected,
            "roi": reward_roi,
            "inverse": True,
            "action": "DoNothing",
            "next": ["QJNN_SigninCompletedBadgeAbsent"],
        }
        if not isinstance(completed_reward_absent, dict) or any(
            completed_reward_absent.get(field) != expected
            for field, expected in expected_completed_reward_absent.items()
        ):
            errors.append(
                "30_signin.json: SigninCompletedRewardAbsent 必须在顶层确认奖励标题消失"
            )

        completed_badge_absent = signin.get("QJNN_SigninCompletedBadgeAbsent", {})
        pending_badge = signin.get("QJNN_SigninPendingBadge", {})
        badge_fields = (
            "recognition",
            "lower",
            "upper",
            "method",
            "count",
            "connected",
            "roi",
            "roi_offset",
        )
        if not isinstance(completed_badge_absent, dict) or any(
            completed_badge_absent.get(field) != pending_badge.get(field)
            for field in badge_fields
        ):
            errors.append(
                "30_signin.json: SigninCompletedBadgeAbsent 必须复用签到待领取红点判据"
            )
        elif completed_badge_absent.get("inverse") is not True:
            errors.append(
                "30_signin.json: SigninCompletedBadgeAbsent 必须确认可领取红点不存在"
            )
        elif completed_badge_absent.get("action") != "DoNothing":
            errors.append(
                "30_signin.json: SigninCompletedBadgeAbsent 只能零点击完成"
            )

        for obsolete in ("QJNN_SigninEntryAbsent", "QJNN_SigninEntryGone"):
            if obsolete in signin:
                errors.append(f"30_signin.json: 已废弃的入口消失判据仍存在：{obsolete}")

        normal_dismiss = signin.get("QJNN_SigninRewardDismiss", {})
        if normal_dismiss.get("next") != ["QJNN_SigninRewardDismissConfirm"]:
            errors.append("30_signin.json: 普通奖励点击后必须先确认奖励标题消失")
        if normal_dismiss.get("post_delay", 0) < 400:
            errors.append("30_signin.json: 普通奖励点击后必须等待 UI 更新")

        dismiss_confirm = signin.get("QJNN_SigninRewardDismissConfirm", {})
        expected_confirm = {
            "recognition": "OCR",
            "expected": reward_expected,
            "roi": reward_roi,
            "inverse": True,
            "action": "DoNothing",
            "next": ["QJNN_SigninRewardFlow"],
            "on_error": ["QJNN_SigninFailure"],
        }
        if not isinstance(dismiss_confirm, dict):
            errors.append("30_signin.json: 缺少奖励标题消失确认节点")
        else:
            for field, expected in expected_confirm.items():
                if dismiss_confirm.get(field) != expected:
                    errors.append(
                        "30_signin.json: SigninRewardDismissConfirm."
                        f"{field} 必须为 {expected!r}"
                    )
            timeout = dismiss_confirm.get("timeout")
            rate_limit = dismiss_confirm.get("rate_limit")
            if not isinstance(timeout, int) or not 1000 <= timeout <= 5000:
                errors.append("30_signin.json: 奖励消失确认必须在 1~5 秒内有限超时")
            if not isinstance(rate_limit, int) or rate_limit < 200:
                errors.append("30_signin.json: 奖励消失确认必须等待并重新取帧")

        suit_safe_rect = [300, 700, 120, 120]
        suit_showcase = signin.get("QJNN_RewardSuitShowcase", {})
        expected_suit_fields = {
            "recognition": "Custom",
            "custom_recognition": "qjnn_reward_suit_showcase",
            "action": "DoNothing",
            "next": ["QJNN_RewardSuitShowcaseDismiss"],
            "on_error": ["QJNN_SigninFailure"],
        }
        if not isinstance(suit_showcase, dict):
            errors.append("30_signin.json: 缺少 QJNN_RewardSuitShowcase")
        else:
            for field, expected in expected_suit_fields.items():
                if suit_showcase.get(field) != expected:
                    errors.append(
                        f"30_signin.json: QJNN_RewardSuitShowcase.{field} "
                        f"必须为 {expected!r}"
                    )
            suit_param = suit_showcase.get("custom_recognition_param", {})
            expected_suit_param = {
                "roi": [0, 250, 720, 930],
                "threshold": 0.8,
                "safe_rect": suit_safe_rect,
                "depends_on_suit_name": False,
                "depends_on_progress_text": False,
            }
            if not isinstance(suit_param, dict):
                errors.append("30_signin.json: RewardSuitShowcase 缺少结构识别参数")
            else:
                for field, expected in expected_suit_param.items():
                    if suit_param.get(field) != expected:
                        errors.append(
                            f"30_signin.json: RewardSuitShowcase.{field} "
                            f"必须为 {expected!r}"
                        )
            serialized = json.dumps(suit_showcase, ensure_ascii=False)
            if "1/6" in serialized or "红裳锦绣" in serialized:
                errors.append("30_signin.json: 套装展示识别不得依赖具体进度或套装名")

        suit_dismiss = signin.get("QJNN_RewardSuitShowcaseDismiss", {})
        if not isinstance(suit_dismiss, dict):
            errors.append("30_signin.json: 缺少 QJNN_RewardSuitShowcaseDismiss")
        else:
            if suit_dismiss.get("recognition") != "DirectHit":
                errors.append("30_signin.json: SuitShowcaseDismiss 必须在确认后直接执行一次")
            if suit_dismiss.get("action") != "Click":
                errors.append("30_signin.json: SuitShowcaseDismiss 必须且只能 Click")
            if suit_dismiss.get("target") != suit_safe_rect:
                errors.append("30_signin.json: SuitShowcaseDismiss 只能点击中部安全区")
            if suit_dismiss.get("target") == [0, 0, 720, 1280]:
                errors.append("30_signin.json: SuitShowcaseDismiss 禁止全屏点击")
            if suit_dismiss.get("repeat", 1) != 1:
                errors.append("30_signin.json: SuitShowcaseDismiss 禁止固定连点")
            if suit_dismiss.get("next") != ["QJNN_SigninRewardFlow"]:
                errors.append("30_signin.json: SuitShowcaseDismiss 点击后必须回 RewardFlow")
            max_hit = suit_dismiss.get("max_hit")
            if not isinstance(max_hit, int) or not 1 <= max_hit <= 15:
                errors.append("30_signin.json: SuitShowcaseDismiss 必须有不超过 15 次的上限")

        clothes_screen = signin.get("QJNN_RewardClothesScreen", {})
        if not isinstance(clothes_screen, dict):
            errors.append("30_signin.json: 缺少 QJNN_RewardClothesScreen")
        else:
            expected_clothes_fields = {
                "recognition": "Custom",
                "custom_recognition": "qjnn_reward_clothes_card",
                "action": "DoNothing",
                "next": ["QJNN_RewardClothesSafeDismiss"],
                "on_error": ["QJNN_SigninFailure"],
            }
            for field, expected in expected_clothes_fields.items():
                if clothes_screen.get(field) != expected:
                    errors.append(
                        f"30_signin.json: QJNN_RewardClothesScreen.{field} 必须为 {expected!r}"
                    )
            clothes_param = clothes_screen.get("custom_recognition_param", {})
            if not isinstance(clothes_param, dict):
                errors.append("30_signin.json: RewardClothesScreen 缺少结构识别参数")
            else:
                expected_geometry = {
                    "roi": [90, 285, 540, 700],
                    "threshold": 0.65,
                    "reward_card_rect": reward_card_rect,
                    "safe_rect": reward_safe_rect,
                    "share_forbidden_rect": share_forbidden_rect,
                    "depends_on_new_obtained": False,
                }
                for field, expected in expected_geometry.items():
                    if clothes_param.get(field) != expected:
                        errors.append(
                            "30_signin.json: RewardClothesScreen "
                            f"{field} 必须为 {expected!r}"
                        )
                serialized = json.dumps(clothes_screen, ensure_ascii=False)
                if "新获得" in serialized or "超稀有" in serialized or "锦心" in serialized:
                    errors.append("30_signin.json: 服装页结构识别不得依赖可变奖励文字")

        safe_dismiss = signin.get("QJNN_RewardClothesSafeDismiss", {})
        if not isinstance(safe_dismiss, dict):
            errors.append("30_signin.json: 缺少 QJNN_RewardClothesSafeDismiss")
        else:
            if safe_dismiss.get("recognition") != "DirectHit":
                errors.append("30_signin.json: SafeDismiss 必须在服装页确认后直接执行一次")
            if safe_dismiss.get("action") != "Click":
                errors.append("30_signin.json: SafeDismiss 的唯一职责必须是 Click")
            if safe_dismiss.get("target") != reward_safe_rect:
                errors.append("30_signin.json: SafeDismiss 只能点击 RewardClothesSafeRect")
            if safe_dismiss.get("next") != ["QJNN_SigninRewardFlow"]:
                errors.append("30_signin.json: SafeDismiss 点击一次后必须重新识别 RewardFlow")
            if safe_dismiss.get("repeat", 1) != 1:
                errors.append("30_signin.json: SafeDismiss 禁止固定连点")
            max_hit = safe_dismiss.get("max_hit")
            if not isinstance(max_hit, int) or not 1 <= max_hit <= 15:
                errors.append("30_signin.json: SafeDismiss 必须有不超过 15 次的点击上限")

        if not rect_contains(reward_card_rect, reward_safe_rect):
            errors.append("30_signin.json: RewardClothesSafeRect 必须完全位于 RewardCardRect 内")
        if rects_intersect(reward_safe_rect, share_forbidden_rect):
            errors.append("30_signin.json: RewardClothesSafeRect 禁止与 ShareForbiddenRect 相交")
        safe_bottom = reward_safe_rect[1] + reward_safe_rect[3]
        if share_forbidden_rect[1] - safe_bottom < 100:
            errors.append("30_signin.json: RewardClothesSafeRect 与分享禁区必须至少间隔 100px")

        for node_name, node in signin.items():
            if not isinstance(node, dict):
                continue
            expected = node.get("expected", [])
            mentions_share = "share" in node_name.lower() or (
                isinstance(expected, list) and "分享" in expected
            )
            if mentions_share and node.get("action") == "Click":
                errors.append(f"30_signin.json: {node_name} 涉及分享，绝对禁止绑定 Click")
            if "Reward" in node_name and node.get("action") == "Click":
                target = node.get("target")
                if target is True or target == [0, 0, 720, 1280]:
                    errors.append(f"30_signin.json: {node_name} 禁止全屏或识别框盲点")

        if 'custom_recognition("qjnn_reward_clothes_card")' not in recognition_source:
            errors.append("qjnn_recognition.py: AgentServer 未注册服装奖励结构识别")
        if 'custom_recognition("qjnn_reward_suit_showcase")' not in recognition_source:
            errors.append("qjnn_recognition.py: AgentServer 未注册套装展示结构识别")

        run_task_source = (ROOT / "tools" / "run_task.py").read_text(encoding="utf-8")
        reward_debug_log = 'print("[SigninRewardDebug] Starting from current screen")'
        if reward_debug_log not in run_task_source:
            errors.append("run_task.py: RewardDebug 提交前必须输出规定的开始日志")
        reward_source = (ROOT / "agent" / "qjnn_reward.py").read_text(encoding="utf-8")
        required_reward_constants = (
            "REWARD_CARD_RECT",
            "REWARD_CLOTHES_SAFE_RECT",
            "SHARE_FORBIDDEN_RECT",
            "RewardClothesRecognition",
            "REWARD_SUIT_SHOWCASE_SAFE_RECT",
            "RewardSuitShowcaseRecognition",
            "detect_reward_suit_showcase",
        )
        for symbol in required_reward_constants:
            if symbol not in reward_source:
                errors.append(f"qjnn_reward.py: 缺少 {symbol}")
        runner_source = (ROOT / "tools" / "run_task.py").read_text(encoding="utf-8")
        if (
            '"qjnn_reward_clothes_card", RewardClothesRecognition()'
            not in runner_source
        ):
            errors.append("run_task.py: 正式 runner 未注册服装奖励结构识别")
        if (
            '"qjnn_reward_suit_showcase", RewardSuitShowcaseRecognition()'
            not in runner_source
        ):
            errors.append("run_task.py: 正式 runner 未注册套装展示结构识别")

        suit_sample = SCREENSHOT_ROOT / "daily" / "reward_suit_showcase_real.png"
        if not suit_sample.is_file():
            missing_input(suit_sample, errors, "缺少真实套装展示离线样本：images/screenshots/daily/reward_suit_showcase_real.png", source_only)

        for node_name, node in signin.items():
            if node_name.startswith("QJNN_Signin") and isinstance(node, dict):
                if node_name not in {
                    "QJNN_SigninEntryText",
                    "QJNN_SigninPendingBadge",
                    "QJNN_SigninSkip",
                    "QJNN_SigninFailure",
                }:
                    if node.get("on_error") != ["QJNN_SigninFailure"]:
                        errors.append(
                            f"30_signin.json: {node_name} 必须有限失败并进入 QJNN_SigninFailure"
                        )
        failure = signin.get("QJNN_SigninFailure", {})
        if not isinstance(failure, dict) or failure.get("action") != "Custom":
            errors.append("30_signin.json: QJNN_SigninFailure 必须保存调试截图")
        elif failure.get("custom_action") != "qjnn_save_signin_failure":
            errors.append(
                "30_signin.json: QJNN_SigninFailure 必须调用 qjnn_save_signin_failure"
            )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"{signin_path.relative_to(ROOT)}: {exc}")

    mail_path = PIPELINE / "50_mail.json"
    try:
        mail = load_json(mail_path)
        mail_entry_text = {
            "recognition": "OCR",
            "expected": ["邮箱"],
            "threshold": 0.3,
        }
        mail_system_text = {
            "recognition": "OCR",
            "expected": ["系统邮件"],
            "threshold": 0.3,
        }
        expected_mail_nodes = {
            "QJNN_MailRewardResume": {
                "recognition": "And",
                "all_of": [
                    {"recognition": "OCR", "expected": ["^邮箱$"],
                     "threshold": 0.8, "roi": [120, 120, 480, 140]},
                    {"recognition": "OCR", "expected": ["^恭喜获得$", "^获得奖励$"],
                     "threshold": 0.8, "roi": [120, 240, 480, 180]},
                    {"recognition": "OCR", "expected": ["^跳过$", "^确定$"],
                     "threshold": 0.8, "roi": [0, 880, 720, 310]},
                ],
                "action": "DoNothing",
                "max_hit": 1,
                "next": ["QJNN_MailRewardFlow"],
                "on_error": ["QJNN_MailFailure"],
            },
            "QJNN_MailHomeScreen": {
                "recognition": "And",
                "all_of": [{"recognition": "Custom", "custom_recognition": "qjnn_home_any_page", "custom_recognition_param": {"strict": True}}],
                "action": "Custom",
                "custom_action": "qjnn_home_navigate",
                "custom_action_param": {"target_page": "P2"},
                "next": ["QJNN_MailEntry", "QJNN_MailSkip"],
            },
            "QJNN_MailEntryText": mail_entry_text,
            "QJNN_MailHomePendingBadge": {
                "recognition": "ColorMatch",
                "roi": "QJNN_MailEntryText",
                "roi_offset": [35, -90, 75, 70],
            },
            "QJNN_MailEntry": {
                "recognition": "And",
                "all_of": [
                    "QJNN_MailEntryText",
                    "QJNN_MailHomePendingBadge",
                ],
                "box_index": 0,
                "action": "Click",
                "target": True,
                "max_hit": 1,
                "next": ["QJNN_MailSystemScreen"],
            },
            "QJNN_MailPageTitle": mail_entry_text,
            "QJNN_MailSystemTabText": mail_system_text,
            "QJNN_MailSystemPendingBadge": {
                "recognition": "ColorMatch",
                "roi": "QJNN_MailSystemTabText",
                "roi_offset": [85, -50, -50, 20],
            },
            "QJNN_MailSystemBadgeGone": {
                "recognition": "ColorMatch",
                "roi": "QJNN_MailSystemTabText",
                "roi_offset": [85, -50, -50, 20],
                "inverse": True,
                "action": "DoNothing",
                "next": ["QJNN_MailSystemDone"],
            },
            "QJNN_MailSystemScreen": {
                "recognition": "And",
                "all_of": ["QJNN_MailPageTitle", "QJNN_MailSystemTabText"],
                "action": "Click",
                "box_index": 1,
                "target": True,
                "max_hit": 1,
                "next": ["QJNN_MailSystemSelected"],
            },
            "QJNN_MailSystemSelected": {
                "recognition": "And",
                "all_of": ["QJNN_MailPageTitle", "QJNN_MailSystemTabText", "QJNN_MailSystemTabSelected"],
                "next": [
                    "QJNN_MailSystemPending",
                    "QJNN_MailSystemBadgeGone",
                ],
            },
            "QJNN_MailSystemPending": {
                "recognition": "And",
                "all_of": [
                    "QJNN_MailSystemTabText",
                    "QJNN_MailSystemPendingBadge",
                    "QJNN_MailSystemTabSelected",
                ],
                "next": ["QJNN_MailClaimAll"],
            },
            "QJNN_MailClaimAll": {
                "recognition": "OCR",
                "expected": ["一键领取", "键领取"],
                "action": "Click",
                "target": True,
                "max_hit": 1,
                "next": ["QJNN_MailRewardFlow", "QJNN_MailNoClaimable"],
            },
            "QJNN_MailRewardSkip": {
                "recognition": "OCR",
                "expected": ["跳过"],
                "action": "DoNothing",
                "max_hit": 1,
            },
            "QJNN_MailRewardConfirm": {
                "recognition": "OCR",
                "expected": ["确定"],
                "action": "DoNothing",
                "max_hit": 1,
            },
            "QJNN_MailNoClaimable": {
                "recognition": "OCR",
                "expected": ["没有可领取的奖励"],
                "action": "DoNothing",
                "next": ["QJNN_MailClose"],
            },
            "QJNN_MailSystemDone": {
                "recognition": "And",
                "all_of": [
                    "QJNN_MailPageTitle",
                    "QJNN_MailSystemTabText",
                    "QJNN_MailSystemTabSelected",
                ],
                "next": ["QJNN_MailClose"],
            },
            "QJNN_MailClose": {
                "recognition": "DirectHit",
                "action": "Click",
                "target": [240, 120, 240, 48],
                "max_hit": 1,
                "next": ["QJNN_MailRewardHomeClear"],
            },
            "QJNN_HomeAfterMail": {
                "recognition": "And",
                "all_of": ["QJNN_HomeScreen"],
                "action": "DoNothing",
            },
            "QJNN_MailRewardSystemReady": {
                "recognition": "And",
                "all_of": ["QJNN_MailPageTitle", "QJNN_MailSystemTabText",
                           "QJNN_MailSystemTabSelected"],
                "action": "DoNothing",
                "next": ["QJNN_MailSystemBadgeGone"],
            },
            "QJNN_MailRewardHomeClear": {
                "recognition": "OCR",
                "expected": ["系统邮件", "邮件奖励已领取", "删除"],
                "inverse": True,
                "next": ["QJNN_HomeAfterMail"],
            },
        }
        for node_name, expected_fields in expected_mail_nodes.items():
            node = mail.get(node_name)
            if not isinstance(node, dict):
                errors.append(f"50_mail.json: 缺少邮箱节点 {node_name}")
                continue
            for field, expected in expected_fields.items():
                if node.get(field) != expected:
                    errors.append(
                        f"50_mail.json: {node_name}.{field} 必须为 {expected!r}"
                    )

        mail_entry = mail.get("QJNN_Mail", {})
        if not isinstance(mail_entry, dict) or mail_entry.get("next") != [
            "QJNN_MailRewardResume",
            "QJNN_MailClothesRewardResume",
            "QJNN_MailSystemScreen",
            "QJNN_MailHomeScreen",
        ]:
            errors.append(
                "50_mail.json: QJNN_Mail 必须先接续已确认的邮箱奖励，再识别系统邮件页和首页"
            )

        mail_skip = mail.get("QJNN_MailSkip", {})
        if not isinstance(mail_skip, dict) or mail_skip.get("recognition") != "DirectHit":
            errors.append("50_mail.json: QJNN_MailSkip 必须是首页分支的正常结束节点")

        # 三条正常路径必须分别收敛到领取后回首页、首页跳过、邮箱内关闭回首页；
        # MailSkip 不得成为邮箱内部已完成状态的出口。
        expected_mail_paths = {
            "遗留邮箱奖励接续": [
                ("QJNN_Mail", "QJNN_MailRewardResume"),
                ("QJNN_MailRewardResume", "QJNN_MailRewardFlow"),
                ("QJNN_MailRewardFlow", "QJNN_MailRewardSystemReady"),
                ("QJNN_MailRewardSystemReady", "QJNN_MailSystemBadgeGone"),
                ("QJNN_MailSystemBadgeGone", "QJNN_MailSystemDone"),
                ("QJNN_MailSystemDone", "QJNN_MailClose"),
                ("QJNN_MailClose", "QJNN_MailRewardHomeClear"),
                ("QJNN_MailRewardHomeClear", "QJNN_HomeAfterMail"),
            ],
            "首页有系统邮件": [
                ("QJNN_Mail", "QJNN_MailHomeScreen"),
                ("QJNN_MailHomeScreen", "QJNN_MailEntry"),
                ("QJNN_MailEntry", "QJNN_MailSystemScreen"),
                ("QJNN_MailSystemScreen", "QJNN_MailSystemSelected"),
                ("QJNN_MailSystemSelected", "QJNN_MailSystemPending"),
                ("QJNN_MailSystemPending", "QJNN_MailClaimAll"),
                ("QJNN_MailSystemDone", "QJNN_MailClose"),
                ("QJNN_MailClose", "QJNN_MailRewardHomeClear"),
                ("QJNN_MailRewardHomeClear", "QJNN_HomeAfterMail"),
            ],
            "首页无系统邮件": [
                ("QJNN_Mail", "QJNN_MailHomeScreen"),
                ("QJNN_MailHomeScreen", "QJNN_MailSkip"),
            ],
            "邮箱页系统邮件已完成": [
                ("QJNN_Mail", "QJNN_MailSystemScreen"),
                ("QJNN_MailSystemScreen", "QJNN_MailSystemSelected"),
                ("QJNN_MailSystemSelected", "QJNN_MailSystemBadgeGone"),
                ("QJNN_MailSystemBadgeGone", "QJNN_MailSystemDone"),
                ("QJNN_MailSystemDone", "QJNN_MailClose"),
                ("QJNN_MailClose", "QJNN_MailRewardHomeClear"),
                ("QJNN_MailRewardHomeClear", "QJNN_HomeAfterMail"),
            ],
        }
        for path_name, edges in expected_mail_paths.items():
            for source, target in edges:
                source_node = mail.get(source, {})
                if not isinstance(source_node, dict) or target not in source_node.get("next", []):
                    errors.append(
                        f"50_mail.json: {path_name} 路径缺少 {source} -> {target}"
                    )

        internal_mail_nodes = {
            "QJNN_MailSystemScreen",
            "QJNN_MailSystemSelected",
            "QJNN_MailSystemPending",
            "QJNN_MailClaimAll",
            "QJNN_MailRewardSkip",
            "QJNN_MailRewardConfirm",
            "QJNN_MailNoClaimable",
            "QJNN_MailSystemBadgeGone",
            "QJNN_MailSystemDone",
            "QJNN_MailClose",
        }
        for node_name in internal_mail_nodes:
            node = mail.get(node_name, {})
            if isinstance(node, dict) and "QJNN_MailSkip" in node.get("next", []):
                errors.append(
                    f"50_mail.json: {node_name} 位于邮箱内部，不得进入 QJNN_MailSkip"
                )

        mail_graph = {
            node_name: [
                target
                for target in node.get("next", [])
                if isinstance(target, str) and target.startswith("QJNN_Mail")
            ]
            for node_name, node in mail.items()
            if node_name.startswith("QJNN_Mail") and isinstance(node, dict)
        }
        mail_visiting: set[str] = set()
        mail_visited: set[str] = set()

        def check_mail_cycle(node_name: str) -> bool:
            if node_name in mail_visiting:
                return True
            if node_name in mail_visited:
                return False
            mail_visiting.add(node_name)
            has_cycle = any(
                check_mail_cycle(target) for target in mail_graph.get(node_name, [])
            )
            mail_visiting.remove(node_name)
            mail_visited.add(node_name)
            return has_cycle

        if check_mail_cycle("QJNN_Mail"):
            errors.append("50_mail.json: QJNN_Mail 正常流程存在循环")

        for node_name, node in mail.items():
            if not node_name.startswith("QJNN_Mail") or not isinstance(node, dict):
                continue
            if node_name not in {
                "QJNN_MailEntryText",
                "QJNN_MailHomePendingBadge",
                "QJNN_MailPageTitle",
                "QJNN_MailSystemTabText",
                "QJNN_MailSystemPendingBadge",
                "QJNN_MailSystemTabSelected",
                "QJNN_MailSkip",
                "QJNN_MailFailure",
            } and node.get("on_error") != ["QJNN_MailFailure"]:
                errors.append(
                    f"50_mail.json: {node_name} 必须有限失败并进入 QJNN_MailFailure"
                )

        failure = mail.get("QJNN_MailFailure", {})
        if not isinstance(failure, dict) or failure.get("action") != "Custom":
            errors.append("50_mail.json: QJNN_MailFailure 必须保存调试截图")
        elif failure.get("custom_action") != "qjnn_save_mail_failure":
            errors.append(
                "50_mail.json: QJNN_MailFailure 必须调用 qjnn_save_mail_failure"
            )

        forbidden_mail_targets = {"好友邮件", "公告", "一键删除", "删除已读"}
        for node_name, node in mail.items():
            if not isinstance(node, dict) or node.get("action") != "Click":
                continue
            expected = node.get("expected", [])
            if isinstance(expected, list) and forbidden_mail_targets.intersection(expected):
                errors.append(f"50_mail.json: {node_name} 禁止点击非系统邮件目标")

        action_source = (ROOT / "agent" / "qjnn_action.py").read_text(encoding="utf-8")
        if 'custom_action("qjnn_save_mail_failure")' not in action_source:
            errors.append("qjnn_action.py: 缺少 qjnn_save_mail_failure 调试截图动作")
        runner_source = (ROOT / "tools" / "run_task.py").read_text(encoding="utf-8")
        if '"qjnn_save_mail_failure", SaveMailFailure()' not in runner_source:
            errors.append("run_task.py: 正式 runner 未注册 qjnn_save_mail_failure")
        if '"QJNN_SafeStop"' not in runner_source or "业务流程未成功" not in runner_source:
            errors.append("run_task.py: 正式 runner 未将 SafeStop 判定为业务失败")
        if (
            'node.name == "QJNN_HomeStableTimeout"' not in runner_source
            or "Home base UI detected, but async home controls did not become ready"
            not in runner_source
        ):
            errors.append("run_task.py: 首页异步控件超时必须输出明确的启动失败诊断")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"{mail_path.relative_to(ROOT)}: {exc}")

    end_to_end_path = PIPELINE / "40_start_and_signin.json"
    try:
        end_to_end = load_json(end_to_end_path)
        check_pipeline_references(end_to_end, set(nodes), end_to_end_path, errors)
        wrapper = end_to_end.get("QJNN_StartAndSignin")
        expected_next = [
            "QJNN_SigninRewardPopup",
            "QJNN_SigninScreenAfterClaim",
            "QJNN_SigninScreen",
            "QJNN_RewardSuitShowcase",
            "QJNN_RewardClothesScreen",
            {"name": "QJNN_StartAndEnterHome", "jump_back": True},
            "QJNN_Signin",
        ]
        if not isinstance(wrapper, dict):
            errors.append("40_start_and_signin.json: 缺少 QJNN_StartAndSignin")
        else:
            expected_wrapper_fields = {
                "recognition": "DirectHit",
                "action": "DoNothing",
                "next": expected_next,
                "on_error": ["QJNN_SafeStop"],
            }
            for field, expected in expected_wrapper_fields.items():
                if wrapper.get(field) != expected:
                    errors.append(
                        f"40_start_and_signin.json: QJNN_StartAndSignin.{field} "
                        f"必须为 {expected!r}"
                    )

        def referenced_names(value: Any) -> list[str]:
            if not isinstance(value, list):
                return []
            result: list[str] = []
            for item in value:
                if isinstance(item, str):
                    result.append(item)
                elif isinstance(item, dict) and isinstance(item.get("name"), str):
                    result.append(item["name"])
            return result

        for node_name, node_path in nodes.items():
            node = load_json(node_path).get(node_name, {})
            if not isinstance(node, dict):
                continue
            for field in ("next", "on_error"):
                for referenced in referenced_names(node.get(field)):
                    if referenced not in nodes:
                        errors.append(
                            f"{node_path.relative_to(ROOT)}: {node_name}.{field} "
                            f"引用不存在的节点 {referenced!r}"
                        )

        # jump_back is a return edge, not a graph edge. The two reused task trees
        # must not route back to the wrapper and form an accidental task cycle.
        graph: dict[str, list[str]] = {}
        for node_name, node_path in nodes.items():
            node = load_json(node_path).get(node_name, {})
            graph[node_name] = referenced_names(node.get("next")) if isinstance(node, dict) else []
        for branch in ("QJNN_StartAndEnterHome", "QJNN_Signin"):
            pending = [branch]
            visited: set[str] = set()
            while pending:
                current = pending.pop()
                if current == "QJNN_StartAndSignin":
                    errors.append(
                        f"40_start_and_signin.json: {branch} 分支循环引用 QJNN_StartAndSignin"
                    )
                    break
                if current in visited:
                    continue
                visited.add(current)
                pending.extend(graph.get(current, []))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"{end_to_end_path.relative_to(ROOT)}: {exc}")

    # wait_for_screen 仅保留给独立 CLI 调试/兼容路径；正式 UI 任务由原生 Pipeline 驱动。
    if "detect_screen(" not in recognition_source or "detect_screen(" not in main_source:
        errors.append("页面状态必须统一通过 detect_screen 识别")

    check_python(errors)

    device_manager_source = (
        ROOT / "agent" / "device_manager.py"
    ).read_text(encoding="utf-8")
    required_alias_fragments = {
        'controller.shell("settings", "get", "secure", "android_id")': (
            "android_id 只读身份查询"
        ),
        'optional_prop("ro.serialno")': "ro.serialno 辅助身份",
        "def same_android_instance(": "保守同实例判定",
        "left.model, left.product, left.device": "model/product/device 联合校验",
        "def deduplicate_device_aliases(": "ADB alias 去重",
        "configured_serial=configured_serial": "配置 alias canonical 优先",
        "is_tcp_serial(device.serial)": "TCP canonical 优先",
        'print("[ADB] alias merged:")': "alias 明确日志",
        "alias merge disabled": "身份不完整时禁止冒险合并",
        'MUMU_PRIMARY_TCP_ENDPOINT = "127.0.0.1:16416"': "MuMu TCP 主别名",
        "def refresh_mumu_devices(": "每次任务前 MuMu TCP 恢复",
        '"[ADB] attempting MuMu TCP reconnect: "': "MuMu TCP 恢复日志",
        "def query_foreground_package(": "QJNN 前台只读检查",
        'controller.shell("dumpsys", *command, timeout=8)': "dumpsys 前台检查",
        'print(f"[ADB] foreground Miracle Nikki found: {selected.serial}")': "奇迹暖暖前台命中日志",
        'print(f"[ADB] selected device: {selected.serial}")': "设备选择日志",
    }
    for fragment, purpose in required_alias_fragments.items():
        if fragment not in device_manager_source:
            errors.append(f"device_manager.py: 缺少{purpose}")
    select_start = device_manager_source.find("def select_device(")
    dedupe_call = device_manager_source.find(
        "usable = deduplicate_device_aliases(", select_start
    )
    package_scan = device_manager_source.find(
        "matches = _matching_package_devices(", select_start
    )
    if not (select_start >= 0 and select_start < dedupe_call < package_scan):
        errors.append("device_manager.py: 必须先合并 alias，再统计安装目标 package 的实例")

    action_source = (ROOT / "agent" / "qjnn_recognition.py").read_text(encoding="utf-8")
    runner_source = (ROOT / "tools" / "run_task.py").read_text(encoding="utf-8")
    if 'custom_recognition("qjnn_package_foreground")' not in action_source:
        errors.append("qjnn_recognition.py: 缺少 qjnn_package_foreground 注册")
    if 'register_custom_recognition(\n        "qjnn_package_foreground"' not in runner_source:
        errors.append("run_task.py: 正式 runner 未注册 qjnn_package_foreground")

    def collect_pipeline_extensions(kind: str) -> set[str]:
        found: set[str] = set()
        discriminator = "recognition" if kind == "recognition" else "action"
        name_field = f"custom_{kind}"

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                if value.get(discriminator) == "Custom":
                    name = value.get(name_field)
                    if isinstance(name, str) and name:
                        found.add(name)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        for _, document in pipeline_documents:
            walk(document)
        return found

    pipeline_custom_recognitions = collect_pipeline_extensions("recognition")
    runtime_custom_recognitions = set(
        re.findall(
            r'register_custom_recognition\(\s*["\']([^"\']+)',
            runner_source,
        )
    )
    agent_custom_recognitions = set(
        re.findall(
            r'custom_recognition\(["\']([^"\']+)',
            action_source,
        )
    )
    missing_runtime_recognitions = (
        pipeline_custom_recognitions - runtime_custom_recognitions
    )
    missing_agent_recognitions = pipeline_custom_recognitions - agent_custom_recognitions
    if missing_runtime_recognitions:
        errors.append(
            "run_task.py: Pipeline CustomRecognition 未在正式 Resource runtime 注册："
            + ", ".join(sorted(missing_runtime_recognitions))
        )
    if missing_agent_recognitions:
        errors.append(
            "qjnn_recognition.py: Pipeline CustomRecognition 未在 AgentServer 注册："
            + ", ".join(sorted(missing_agent_recognitions))
        )

    pipeline_custom_actions = collect_pipeline_extensions("action")
    runtime_custom_actions = set(
        re.findall(
            r'register_custom_action\(\s*["\']([^"\']+)',
            runner_source,
        )
    )
    agent_action_registry_source = (
        ROOT / "agent" / "qjnn_action.py"
    ).read_text(encoding="utf-8")
    agent_custom_actions = set(
        re.findall(
            r'custom_action\(["\']([^"\']+)',
            agent_action_registry_source,
        )
    )
    missing_runtime_actions = pipeline_custom_actions - runtime_custom_actions
    missing_agent_actions = pipeline_custom_actions - agent_custom_actions
    if missing_runtime_actions:
        errors.append(
            "run_task.py: Pipeline CustomAction 未在正式 Resource runtime 注册："
            + ", ".join(sorted(missing_runtime_actions))
        )
    if missing_agent_actions:
        errors.append(
            "qjnn_action.py: Pipeline CustomAction 未在 AgentServer 注册："
            + ", ".join(sorted(missing_agent_actions))
        )

    privacy_source = (ROOT / "agent" / "qjnn_privacy.py").read_text(encoding="utf-8")
    required_privacy_fragments = {
        "Tasker.set_save_on_error(False)": "进入隐私等待前关闭错误截图",
        "Tasker.set_save_on_error(True)": "返回安全游戏界面后恢复错误截图",
        "controller.post_screencap().wait()": "仅在内存中轮询画面",
        '"start/start_button.png"': "点击开始安全模板",
        '"start/enter_game_button.png"': "进入游戏安全模板",
        '"daily/signin_entry.png"': "主页安全模板",
        "TEMPLATE_REFERENCE_SHORT_SIDE = 900": "安全模板短边缩放基准",
        "normalized_scale = frame_short_side / TEMPLATE_REFERENCE_SHORT_SIDE": "分辨率归一化匹配",
        "if known_third_party:": "明确的第三方前台包确认",
        "登录等待超时，未保存隐私页面截图": "无图超时状态",
    }
    for fragment, purpose in required_privacy_fragments.items():
        if fragment not in privacy_source:
            errors.append(f"qjnn_privacy.py: 缺少{purpose}")

    foreground_source = (ROOT / "agent" / "qjnn_foreground.py").read_text(
        encoding="utf-8"
    )
    required_foreground_fragments = {
        '"activity", "activities"': "Android 15 resumed activity 查询",
        '"window", "windows"': "旧版 focused window 回退查询",
        "actual_package == expected_package": "前台包精确匹配",
        "box=[0, 0, 1, 1] if matched else None": "未知/不匹配状态 fail-closed",
    }
    for fragment, purpose in required_foreground_fragments.items():
        if fragment not in foreground_source:
            errors.append(f"qjnn_foreground.py: 缺少{purpose}")

    forbidden_privacy_fragments = {
        "cv2.imencode": "图片编码",
        "cv2.imwrite": "图片写盘",
        ".tofile(": "数组写盘",
        "write_bytes(": "二进制写盘",
        "write_text(": "文本写盘",
        "shutil.copy": "截图复制",
        "context.run_recognition": "Pipeline OCR/识别调用",
        "requests": "网络请求",
        "httpx": "网络请求",
        "websocket": "网络传输",
        "webhook": "网络传输",
    }
    for fragment, purpose in forbidden_privacy_fragments.items():
        if fragment in privacy_source:
            errors.append(f"qjnn_privacy.py: 隐私等待禁止{purpose}（{fragment}）")

    if 'qjnn_private_login_wait", PrivateLoginWaitAction()' not in runner_source:
        errors.append("run_task.py: 正式 runner 未注册 qjnn_private_login_wait")
    if (
        '"qjnn_private_login_foreground", PrivateLoginForegroundRecognition()'
        not in runner_source
    ):
        errors.append("run_task.py: 正式 runner 未注册 qjnn_private_login_foreground")
    if "finally:\n        # Private-login timeout" not in runner_source:
        errors.append("run_task.py: 任务结束后未恢复 save_on_error 默认值")

    agent_action_source = (ROOT / "agent" / "qjnn_action.py").read_text(encoding="utf-8")
    if 'AgentServer.custom_action("qjnn_private_login_wait")' not in agent_action_source:
        errors.append("qjnn_action.py: AgentServer 未注册 qjnn_private_login_wait")
    if (
        'AgentServer.custom_recognition("qjnn_private_login_foreground")'
        not in action_source
    ):
        errors.append("qjnn_recognition.py: AgentServer 未注册隐私前台检测")

    friend_tasks = [
        task
        for task in tasks
        if isinstance(task, dict) and task.get("entry") == "QJNN_FriendSendHeart"
    ]
    if len(friend_tasks) != 1 or friend_tasks[0].get("name") != "好友一键送心":
        errors.append("interface.json: 必须且只能有一个“好友一键送心”正式任务入口")

    friend_pipeline_path = PIPELINE / "89_friend_send_heart.json"
    try:
        friend_pipeline = load_json(friend_pipeline_path)
        required_friend_nodes = {
            "QJNN_FriendSendHeart",
            "QJNN_FriendHomeAnyPage",
            "QJNN_FriendHomeAnyPageDebug",
            "QJNN_FriendEntry",
            "QJNN_FriendScreenTitle",
            "QJNN_FriendSendHeartText",
            "QJNN_FriendScreen",
            "QJNN_FriendSuccessResult",
            "QJNN_FriendNoTargetText",
            "QJNN_FriendNoTargetResult",
            "QJNN_FriendSendHeartClick",
            "QJNN_FriendSendHeartWaitingResult",
            "QJNN_FriendSendHeartSuccess",
            "QJNN_FriendBack",
            "QJNN_FriendBackDebug",
            "QJNN_HomeAfterFriendSendHeart",
        }
        missing_friend_nodes = required_friend_nodes - set(friend_pipeline)
        if missing_friend_nodes:
            errors.append(
                "89_friend_send_heart.json: 缺少节点 "
                + ", ".join(sorted(missing_friend_nodes))
            )
        if friend_pipeline.get("QJNN_FriendEntry", {}).get("expected") != ["好友"]:
            errors.append("89_friend_send_heart.json: 好友入口必须使用 OCR“好友”")
        if friend_pipeline.get("QJNN_FriendScreen", {}).get("all_of") != [
            "QJNN_FriendScreenTitle",
            "QJNN_FriendSendHeartText",
        ]:
            errors.append("89_friend_send_heart.json: 好友页必须组合识别稳定标题与送心按钮")
        send_heart_text = friend_pipeline.get("QJNN_FriendSendHeartText", {})
        expected_send_heart_variants = [
            {
                "recognition": "OCR",
                "expected": ["一键送心"],
                "threshold": 0.3,
            },
            {
                "recognition": "OCR",
                "expected": ["键送心"],
                "threshold": 0.3,
            },
        ]
        if (
            send_heart_text.get("recognition") != "Or"
            or send_heart_text.get("any_of") != expected_send_heart_variants
        ):
            errors.append(
                "89_friend_send_heart.json: 送心按钮只能接受“一键送心”或“键送心”"
            )
        if friend_pipeline.get("QJNN_FriendSendHeartClick", {}).get("max_hit") != 1:
            errors.append("89_friend_send_heart.json: 一键送心点击必须限制 max_hit=1")
        if friend_pipeline.get("QJNN_FriendSendHeart", {}).get("next") != [
            "QJNN_FriendSuccessResult",
            "QJNN_FriendNoTargetResult",
            "QJNN_FriendScreen",
            "QJNN_FriendHomeAnyPage",
        ]:
            errors.append(
                "89_friend_send_heart.json: 正式重启入口必须依次识别已知结果、好友页和 HOME"
            )
        success_result = friend_pipeline.get("QJNN_FriendSuccessResult", {})
        if (
            success_result.get("recognition") != "Custom"
            or success_result.get("custom_recognition")
            != "qjnn_friend_send_heart_result"
            or success_result.get("custom_recognition_param") != {"kind": "SUCCESS"}
            or success_result.get("next") != ["QJNN_FriendBack"]
        ):
            errors.append(
                "89_friend_send_heart.json: 动态人数成功结果必须使用严格 CustomRecognition 后零点击返回"
            )
        no_target = friend_pipeline.get("QJNN_FriendNoTargetResult", {})
        if no_target.get("all_of") != [
            "QJNN_FriendScreenTitle",
            "QJNN_FriendSendHeartText",
            "QJNN_FriendNoTargetText",
        ] or no_target.get("next") != ["QJNN_FriendBack"]:
            errors.append(
                "89_friend_send_heart.json: 实机无目标提示必须在好友页上下文中零点击返回"
            )
        no_target_text = friend_pipeline.get("QJNN_FriendNoTargetText", {})
        if no_target_text.get("expected") != ["目前没有可以送心的好友"]:
            errors.append("89_friend_send_heart.json: 无目标提示必须使用实机真实文案")
        if friend_pipeline.get("QJNN_FriendSendHeartWaitingResult", {}).get(
            "next"
        ) != ["QJNN_FriendSendHeartSuccess"]:
            errors.append("89_friend_send_heart.json: 等待结果只能进入成功节点")
        friend_back = friend_pipeline.get("QJNN_FriendBack", {})
        if (
            friend_back.get("action") != "Custom"
            or friend_back.get("custom_action") != "qjnn_friend_back"
            or friend_back.get("max_hit") != 1
            or friend_back.get("next") != ["QJNN_HomeAfterFriendSendHeart"]
        ):
            errors.append("89_friend_send_heart.json: FriendBack 必须单击页面内箭头后验证 HOME")
        if "QJNN_Back" in json.dumps(friend_back, ensure_ascii=False):
            errors.append("89_friend_send_heart.json: FriendBack 禁止调用 Android QJNN_Back")
        for node_name in (
            "QJNN_FriendHomeAnyPage",
            "QJNN_FriendHomeAnyPageDebug",
            "QJNN_HomeAfterFriendSendHeart",
        ):
            node = friend_pipeline.get(node_name, {})
            if node.get("custom_recognition") != "qjnn_home_any_page":
                errors.append(f"89_friend_send_heart.json: {node_name} 必须复用 HomeAnyPage")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"{friend_pipeline_path.relative_to(ROOT)}: {exc}")

    friend_source = (ROOT / "agent" / "qjnn_friend_send_heart.py").read_text(
        encoding="utf-8"
    )
    friend_requirements = {
        'FRIEND_PAGE_TITLE = "好友一览"': "好友页标题 OCR",
        'FRIEND_SEND_HEART_TEXT = "一键送心"': "一键送心 OCR",
        'FRIEND_SEND_HEART_OCR_TEXTS = (FRIEND_SEND_HEART_TEXT, "键送心")': (
            "实机键送心 OCR fallback"
        ),
        'FRIEND_NO_TARGET_TEXT = "目前没有可以送心的好友"': "实机无目标结果文案",
        'FRIEND_SUCCESS_PREFIX = "体力已成功发送给"': "实机成功文案前缀",
        'FRIEND_SUCCESS_SUFFIX = "个好友"': "实机成功文案后缀",
        "classify_send_heart_result": "SUCCESS/NO_TARGET/UNKNOWN 严格分类",
        "detect_home_page(argv.image)": "P1/P2/P3 既有分页识别复用",
        'context.run_recognition("QJNN_HomeScreen"': "既有 HOME 组合识别复用",
        '"expected": ["好友"]': "好友任务专用 HOME 入口 OCR",
        "matched = page_matched and friend_matched": "P1/P2/P3 与好友入口组合条件",
        "FriendSendHeartAttempt": "单次点击状态记录",
        "session.sleep(min(interval, remaining))": "点击后先等待再取新帧",
        "diagnostic-only new OCR text": "动态 OCR 差分仅用于诊断",
        "不重复点击": "结果等待阶段防重点击日志",
        "def click_friend_back_once(": "好友页专用返回动作",
        "FRIEND_BACK_SAFE_RECT = (30, 25, 80, 80)": "好友返回 SafeRect",
        "FRIEND_BACK_TEMPLATE_THRESHOLD = 0.80": "好友返回模板实机阈值",
        "arena_back_button_score(frame)": "既有粉色圆形返回模板复用",
    }
    for fragment, purpose in friend_requirements.items():
        if fragment not in friend_source:
            errors.append(f"qjnn_friend_send_heart.py: 缺少{purpose}")
    wait_block = re.search(
        r"def wait_for_send_heart_result\(.*?(?=\n\ndef )",
        friend_source,
        flags=re.DOTALL,
    )
    if wait_block is None or "session.click(" in wait_block.group(0):
        errors.append("qjnn_friend_send_heart.py: WaitingResult 阶段禁止任何点击")
    if 'AgentServer.custom_action("qjnn_friend_send_heart_click")' not in agent_action_source:
        errors.append("qjnn_action.py: 未注册好友送心单击 action")
    if 'AgentServer.custom_action("qjnn_friend_send_heart_wait_result")' not in agent_action_source:
        errors.append("qjnn_action.py: 未注册好友送心结果等待 action")
    if 'AgentServer.custom_recognition("qjnn_home_any_page")' not in action_source:
        errors.append("qjnn_recognition.py: 未注册 HomeAnyPage recognition")
    if 'AgentServer.custom_recognition("qjnn_friend_send_heart_result")' not in action_source:
        errors.append("qjnn_recognition.py: 未注册好友送心结果 recognition")
    if 'AgentServer.custom_action("qjnn_friend_back")' not in agent_action_source:
        errors.append("qjnn_action.py: 未注册好友页内返回 action")
    for fragment in (
        '"qjnn_friend_send_heart_click", FriendSendHeartClickAction()',
        '"qjnn_friend_send_heart_wait_result",',
        '"qjnn_home_any_page", HomeAnyPageRecognition()',
        '"qjnn_friend_send_heart_result", FriendSendHeartResultRecognition()',
        '"qjnn_friend_back", FriendBackAction()',
    ):
        if fragment not in runner_source:
            errors.append(f"run_task.py: 缺少好友送心扩展注册 {fragment}")

    home_navigation_path = PIPELINE / "60_home_navigation.json"
    try:
        home_navigation_pipeline = load_json(home_navigation_path)
        expected_home_entries = {
            "QJNN_HomeNavigateToP1": "P1",
            "QJNN_HomeNavigateToP2": "P2",
            "QJNN_HomeNavigateToP3": "P3",
            "QJNN_HomeToP3": "P3",
        }
        for node_name, target_page in expected_home_entries.items():
            node = home_navigation_pipeline.get(node_name, {})
            expected_node = {
                "recognition": "DirectHit",
                "action": "Custom",
                "custom_action": "qjnn_home_navigate",
                "custom_action_param": {"target_page": target_page},
                "max_hit": 1,
                "timeout": 45000,
                "on_error": ["QJNN_SafeStop"],
            }
            if not isinstance(node, dict) or not node:
                errors.append(f"60_home_navigation.json: 缺少 {node_name}")
                continue
            for field, expected in expected_node.items():
                if node.get(field) != expected:
                    errors.append(
                        f"60_home_navigation.json: {node_name}.{field} "
                        f"必须为 {expected!r}"
                    )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"{home_navigation_path.relative_to(ROOT)}: {exc}")

    home_navigation_source = (
        ROOT / "agent" / "qjnn_home_navigation.py"
    ).read_text(encoding="utf-8")
    required_home_navigation_fragments = {
        "detect_home_page": "复用主页分页识别",
        "HOME_LEFT_ARROW_REFERENCE_POINT = (35, 1100)": "左箭头参考点击点",
        "HOME_RIGHT_ARROW_REFERENCE_POINT = (865, 1100)": "右箭头参考点击点",
        "HOME_PAGE_TRANSITION_WAIT_SECONDS = 0.8": "页面切换等待",
        "HOME_PAGE_UNKNOWN_RETRIES = 3": "Unknown 重试上限",
        "HOME_PAGE_MAX_STEPS = 2": "任意主页之间最多两步",
        "def navigate_home_page(": "项目级通用主页导航 API",
        "expected_page = HOME_PAGES[current_index + direction]": "逐页状态转换",
        "if next_page != expected_page:": "点击后重新识别并校验",
        'log("主页状态识别失败：Unknown")': "Unknown 安全失败日志",
        'log("主页导航失败：超过最大点击次数")': "点击上限失败日志",
    }
    for fragment, purpose in required_home_navigation_fragments.items():
        if fragment not in home_navigation_source:
            errors.append(f"qjnn_home_navigation.py: 缺少{purpose}")
    if 'qjnn_home_navigate", HomeNavigateAction()' not in runner_source:
        errors.append("run_task.py: 正式 runner 未注册 qjnn_home_navigate")
    if 'qjnn_home_to_p3", HomeNavigateAction()' not in runner_source:
        errors.append("run_task.py: 正式 runner 未注册 qjnn_home_to_p3")
    if 'AgentServer.custom_action("qjnn_home_navigate")' not in agent_action_source:
        errors.append("qjnn_action.py: AgentServer 未注册 qjnn_home_navigate")
    if 'AgentServer.custom_action("qjnn_home_to_p3")' not in agent_action_source:
        errors.append("qjnn_action.py: AgentServer 未注册 qjnn_home_to_p3")

    alliance_pipeline_path = PIPELINE / "70_alliance_commission.json"
    try:
        alliance_pipeline = load_json(alliance_pipeline_path)
        alliance_entry = alliance_pipeline.get("QJNN_AllianceCommission", {})
        map_exit = alliance_pipeline.get("QJNN_AllianceCommissionMapExit", {})
        settlement_debug = alliance_pipeline.get(
            "QJNN_AllianceCommissionSettlementDebug", {}
        )
        expected_alliance_entry = {
            "recognition": "DirectHit",
            "action": "Custom",
            "custom_action": "qjnn_alliance_commission",
            "max_hit": 1,
            "timeout": 120000,
            "on_error": ["QJNN_SafeStop"],
        }
        if not isinstance(alliance_entry, dict):
            errors.append("70_alliance_commission.json: 缺少 QJNN_AllianceCommission")
        else:
            for field, expected in expected_alliance_entry.items():
                if alliance_entry.get(field) != expected:
                    errors.append(
                        f"70_alliance_commission.json: QJNN_AllianceCommission.{field} "
                        f"必须为 {expected!r}"
                    )
        expected_map_exit = {
            "recognition": "DirectHit",
            "action": "Custom",
            "custom_action": "qjnn_alliance_commission_map_exit",
            "max_hit": 1,
            "timeout": 20000,
            "on_error": ["QJNN_SafeStop"],
        }
        if not isinstance(map_exit, dict) or not map_exit:
            errors.append(
                "70_alliance_commission.json: 缺少 QJNN_AllianceCommissionMapExit"
            )
        else:
            for field, expected in expected_map_exit.items():
                if map_exit.get(field) != expected:
                    errors.append(
                        "70_alliance_commission.json: "
                        f"QJNN_AllianceCommissionMapExit.{field} 必须为 {expected!r}"
                    )
        expected_settlement_debug = {
            "recognition": "DirectHit",
            "action": "Custom",
            "custom_action": "qjnn_alliance_commission_settlement_debug",
            "max_hit": 1,
            "timeout": 70000,
            "on_error": ["QJNN_SafeStop"],
        }
        if not isinstance(settlement_debug, dict) or not settlement_debug:
            errors.append(
                "70_alliance_commission.json: 缺少 "
                "QJNN_AllianceCommissionSettlementDebug"
            )
        else:
            for field, expected in expected_settlement_debug.items():
                if settlement_debug.get(field) != expected:
                    errors.append(
                        "70_alliance_commission.json: "
                        f"QJNN_AllianceCommissionSettlementDebug.{field} "
                        f"必须为 {expected!r}"
                    )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"{alliance_pipeline_path.relative_to(ROOT)}: {exc}")

    alliance_source = (
        ROOT / "agent" / "qjnn_alliance_commission.py"
    ).read_text(encoding="utf-8")
    required_alliance_fragments = {
        "ALLIANCE_OCR_ATTEMPTS = 3": "关键 OCR 三次尝试上限",
        "ALLIANCE_MAX_COMMISSION_LOOPS = 5": "委托五次循环上限",
        'f"完成{remaining_count}次"': "按地图剩余次数构造完成按钮文字",
        "if last_remaining == 1:": "完成1次的剩余次数硬保护",
        "complete_one_is_gray(detail_frame, complete_box)": "灰色完成1次辅助保护",
        "if detail_popup_visible(map_items):": "0/5 误入详情保护",
        "COMMISSION_BACK_THRESHOLD = 0.80": "返回箭头模板阈值",
        'find_text_box(items, "过关完成")': "过关完成 OCR 确认",
        "ALLIANCE_SETTLEMENT_TIMEOUT_SECONDS = 20.0": "结算等待 20 秒上限",
        "ALLIANCE_SETTLEMENT_POLL_SECONDS = 0.5": "结算轮询 0.5 秒间隔",
        "def wait_for_commission_settlement(": "只读结算等待状态",
        "def run_alliance_commission_settlement_flow(": "正式与调试入口共享的结算状态机",
        'capture("QJNN_AllianceCommissionSettlement", settlement_frame)': "关闭前采集结算页",
        'capture("QJNN_AllianceCommissionPostSettlementPopup", layer_frame)': "关闭前采集第二层",
        "class AllianceCommissionSettlementDebugAction(CustomAction)": "结算现场薄调试入口",
        "def exit_commission_map(": "正式与续跑入口共享的委托地图安全返回 helper",
        "required_remaining_count=0": "续跑前 0/5 硬确认",
        "class AllianceCommissionMapExitAction(CustomAction)": "委托地图安全返回薄 action",
        "super().__init__()": "AllianceCommissionAction 基类初始化",
    }
    for fragment, purpose in required_alliance_fragments.items():
        if fragment not in alliance_source:
            errors.append(f"qjnn_alliance_commission.py: 缺少{purpose}")
    if (
        'qjnn_alliance_commission", AllianceCommissionAction(alliance_capture)'
        not in runner_source
    ):
        errors.append("run_task.py: 正式 runner 未注册 qjnn_alliance_commission")
    if (
        '"qjnn_alliance_commission_settlement_debug",'
        not in runner_source
        or "AllianceCommissionSettlementDebugAction(alliance_capture)"
        not in runner_source
    ):
        errors.append("run_task.py: 未注册联盟委托结算调试 action")
    if (
        'task_name == "QJNN_AllianceCommissionSettlementDebug"'
        not in runner_source
        or "[AllianceCommissionSettlementDebug] Starting from current screen"
        not in runner_source
    ):
        errors.append("run_task.py: 联盟委托结算调试入口缺少明确启动日志")
    if (
        '"qjnn_alliance_commission_map_exit",'
        not in runner_source
        or "AllianceCommissionMapExitAction()" not in runner_source
    ):
        errors.append("run_task.py: 未注册联盟委托地图安全返回 action")
    if (
        'task_name == "QJNN_AllianceAfterCommissionDebug"'
        not in runner_source
        or "[AllianceAfterCommissionDebug] Starting from current commission map"
        not in runner_source
    ):
        errors.append("run_task.py: 联盟后半段续跑调试入口缺少明确启动日志")
    if (
        'AgentServer.custom_action("qjnn_alliance_commission")'
        not in agent_action_source
    ):
        errors.append("qjnn_action.py: AgentServer 未注册 qjnn_alliance_commission")
    if (
        'AgentServer.custom_action("qjnn_alliance_commission_settlement_debug")'
        not in agent_action_source
    ):
        errors.append("qjnn_action.py: AgentServer 未注册联盟委托结算调试 action")
    if (
        'AgentServer.custom_action("qjnn_alliance_commission_map_exit")'
        not in agent_action_source
    ):
        errors.append("qjnn_action.py: AgentServer 未注册联盟委托地图安全返回 action")
    if "ALLIANCE_SETTLEMENT_WAIT_SECONDS" in alliance_source:
        errors.append("qjnn_alliance_commission.py: 禁止恢复固定结算 sleep 作为唯一等待")

    library_pipeline_path = PIPELINE / "80_library.json"
    try:
        library_pipeline = load_json(library_pipeline_path)
        library_entry = library_pipeline.get("QJNN_Library", {})
        expected_library_entry = {
            "recognition": "DirectHit",
            "action": "Custom",
            "custom_action": "qjnn_library",
            "max_hit": 1,
            "timeout": 60000,
            "on_error": ["QJNN_SafeStop"],
        }
        if not isinstance(library_entry, dict):
            errors.append("80_library.json: 缺少 QJNN_Library")
        else:
            for field, expected in expected_library_entry.items():
                if library_entry.get(field) != expected:
                    errors.append(
                        f"80_library.json: QJNN_Library.{field} 必须为 {expected!r}"
                    )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"{library_pipeline_path.relative_to(ROOT)}: {exc}")

    library_source = (ROOT / "agent" / "qjnn_library.py").read_text(encoding="utf-8")
    required_library_fragments = {
        "LIBRARY_OCR_ATTEMPTS = 3": "关键 OCR 三次尝试上限",
        'find_text_box(items, "免费完成", roi=roi)': "小 ROI 免费完成判定",
        'find_text_box(\n        items,\n        "恭喜获得"': "奖励弹窗 OCR 确认",
        'log("未检测到免费完成：不点击整理资料")': "无免费次数付费保护",
        'log("点击整理资料后未识别到恭喜获得；不重复点击整理资料")': "整理资料单击保护",
        "LIBRARY_TOP_BACK_THRESHOLD = 0.80": "顶部返回箭头阈值",
        "super().__init__()": "LibraryAction 基类初始化",
    }
    for fragment, purpose in required_library_fragments.items():
        if fragment not in library_source:
            errors.append(f"qjnn_library.py: 缺少{purpose}")
    if 'qjnn_library", LibraryAction(alliance_capture)' not in runner_source:
        errors.append("run_task.py: 正式 runner 未注册 qjnn_library")
    if 'AgentServer.custom_action("qjnn_library")' not in agent_action_source:
        errors.append("qjnn_action.py: AgentServer 未注册 qjnn_library")

    alliance_daily_path = PIPELINE / "85_alliance_daily.json"
    try:
        alliance_daily = load_json(alliance_daily_path)
        wrapper = alliance_daily.get("QJNN_AllianceDaily", {})
        after_commission_debug = alliance_daily.get(
            "QJNN_AllianceAfterCommissionDebug", {}
        )
        expected_wrapper = {
            "recognition": "DirectHit",
            "action": "Custom",
            "custom_action": "qjnn_alliance_daily",
            "max_hit": 1,
            "timeout": 240000,
            "on_error": ["QJNN_SafeStop"],
        }
        if not isinstance(wrapper, dict):
            errors.append("85_alliance_daily.json: 缺少 QJNN_AllianceDaily")
        else:
            for field, expected in expected_wrapper.items():
                if wrapper.get(field) != expected:
                    errors.append(
                        f"85_alliance_daily.json: QJNN_AllianceDaily.{field} "
                        f"必须为 {expected!r}"
                    )
            if wrapper.get("next"):
                errors.append(
                    "85_alliance_daily.json: 状态恢复入口不得保留无条件 HOME 子任务链"
                )

        expected_after_commission_debug = {
            "recognition": "DirectHit",
            "action": "DoNothing",
            "next": [
                {"name": "QJNN_AllianceCommissionMapExit", "jump_back": True},
                {"name": "QJNN_Library", "jump_back": True},
                "QJNN_AllianceDailyExit",
            ],
            "on_error": ["QJNN_SafeStop"],
        }
        if not isinstance(after_commission_debug, dict) or not after_commission_debug:
            errors.append(
                "85_alliance_daily.json: 缺少 QJNN_AllianceAfterCommissionDebug"
            )
        else:
            for field, expected in expected_after_commission_debug.items():
                if after_commission_debug.get(field) != expected:
                    errors.append(
                        "85_alliance_daily.json: "
                        f"QJNN_AllianceAfterCommissionDebug.{field} 必须为 {expected!r}"
                    )
            if "QJNN_AllianceCommission" in json.dumps(
                after_commission_debug.get("next", []), ensure_ascii=False
            ).replace("QJNN_AllianceCommissionMapExit", ""):
                errors.append(
                    "85_alliance_daily.json: 后半段调试入口禁止重新执行 Commission"
                )

        exit_node = alliance_daily.get("QJNN_AllianceDailyExit", {})
        expected_exit = {
            "recognition": "DirectHit",
            "action": "Custom",
            "custom_action": "qjnn_alliance_daily_exit",
            "max_hit": 1,
            "timeout": 20000,
            "on_error": ["QJNN_SafeStop"],
        }
        if not isinstance(exit_node, dict):
            errors.append("85_alliance_daily.json: 缺少 QJNN_AllianceDailyExit")
        else:
            for field, expected in expected_exit.items():
                if exit_node.get(field) != expected:
                    errors.append(
                        f"85_alliance_daily.json: QJNN_AllianceDailyExit.{field} "
                        f"必须为 {expected!r}"
                    )

        if "QJNN_AllianceDailyPreflight" in alliance_daily:
            errors.append("85_alliance_daily.json: 不应保留业务专用主页分页预检")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"{alliance_daily_path.relative_to(ROOT)}: {exc}")

    alliance_daily_source = (
        ROOT / "agent" / "qjnn_alliance_daily.py"
    ).read_text(encoding="utf-8")
    required_daily_fragments = {
        "find_alliance_home(items, candidate)": "退出前联盟首页确认",
        "top_back_arrow_score": "联盟顶部返回模板复用",
        'last_page == "P3"': "退出后主页 P3 确认",
        'return "COMMISSION_PAGE"': "委托章节恢复判据",
        'return "LIBRARY_PAGE"': "图书馆恢复判据",
        'return "ALLIANCE_HOME"': "联盟主页恢复判据",
        'return "HOME"': "HOME 恢复判据",
        "run_alliance_commission_resume(": "委托页直接续跑",
        "run_alliance_commission_from_home(": "联盟主页直接续跑",
        'capture_unknown("QJNN_AllianceDailyUnknownStart"': "未知入口留档",
        "未返回 HOME、未执行任何猜测点击": "未知状态零重置保护",
    }
    for fragment, purpose in required_daily_fragments.items():
        if fragment not in alliance_daily_source:
            errors.append(f"qjnn_alliance_daily.py: 缺少{purpose}")
    if 'qjnn_alliance_daily_exit", AllianceDailyExitAction()' not in runner_source:
        errors.append("run_task.py: 正式 runner 未注册 qjnn_alliance_daily_exit")
    if 'qjnn_alliance_daily", AllianceDailyAction(alliance_capture)' not in runner_source:
        errors.append("run_task.py: 正式 runner 未注册 qjnn_alliance_daily")
    if (
        'AgentServer.custom_action("qjnn_alliance_daily_exit")'
        not in agent_action_source
    ):
        errors.append("qjnn_action.py: AgentServer 未注册 qjnn_alliance_daily_exit")
    if 'AgentServer.custom_action("qjnn_alliance_daily")' not in agent_action_source:
        errors.append("qjnn_action.py: AgentServer 未注册 qjnn_alliance_daily")
    if "run_alliance_daily_preflight" in alliance_daily_source:
        errors.append("qjnn_alliance_daily.py: 重复的业务专用主页分页预检尚未移除")
    if "qjnn_alliance_daily_preflight" in runner_source or (
        "qjnn_alliance_daily_preflight" in agent_action_source
    ):
        errors.append("联盟主页导航必须只使用项目级 qjnn_home_navigate")

    alliance_capture_source = (
        ROOT / "agent" / "qjnn_alliance_capture.py"
    ).read_text(encoding="utf-8")
    expected_capture_targets = (
        "QJNN_AllianceCommissionMapActive",
        "QJNN_AllianceCommissionDetailActive",
        "QJNN_AllianceCommissionSettlement",
        "QJNN_AllianceCommissionPostSettlementPopup",
        "QJNN_AllianceLibraryFree",
        "QJNN_AllianceLibraryReward",
        "QJNN_AllianceDailyUnknownStart",
    )
    for target in expected_capture_targets:
        if target not in alliance_capture_source:
            errors.append(f"qjnn_alliance_capture.py: 缺少采集状态 {target}")
    expected_capture_symbols = (
        'ALLIANCE_CAPTURE_DIR = DATA_ROOT / "images" / "screenshots" / "alliance"',
        "class AllianceCaptureManager",
        "def capture_once",
        "self._attempted",
        'cv2.imencode(".png", frame)',
    )
    for symbol in expected_capture_symbols:
        if symbol not in alliance_capture_source:
            errors.append(f"qjnn_alliance_capture.py: 缺少一次性原图采集约束 {symbol}")
    lowered_capture_source = alliance_capture_source.lower()
    if "post_connection" in lowered_capture_source or "adb" in lowered_capture_source:
        errors.append("qjnn_alliance_capture.py: 截图采集禁止创建 ADB/Controller 连接")
    if "assets/resource/image" in lowered_capture_source:
        errors.append("qjnn_alliance_capture.py: 原始联盟截图禁止写入模板目录")

    runner_capture_requirements = (
        '"--capture-alliance"',
        "capture_alliance: bool = False",
        "AllianceCaptureManager()",
        "alliance_capture=alliance_capture",
    )
    for symbol in runner_capture_requirements:
        if symbol not in runner_source:
            errors.append(f"run_task.py: 缺少联盟采集开关约束 {symbol}")

    capture_before_click_pairs = (
        (
            alliance_source,
            'capture("QJNN_AllianceCommissionMapActive", map_frame)',
            "_stable_commission_click(session, active_target, position_tolerance=node_tolerance)",
        ),
        (
            alliance_source,
            'capture("QJNN_AllianceCommissionDetailActive", detail_frame)',
            "_stable_commission_click(session, quick_target)",
        ),
        (
            alliance_source,
            'capture("QJNN_AllianceCommissionSettlement", settlement_frame)',
            "_stable_commission_click(session, lambda i,f: _outside_target(\n            i,f,SETTLEMENT_OUTSIDE_FIRST_POINT",
        ),
        (
            alliance_source,
            'capture("QJNN_AllianceCommissionPostSettlementPopup", layer_frame)',
            "_stable_commission_click(session, lambda i,f: _outside_target(\n            i,f,SETTLEMENT_OUTSIDE_SECOND_POINT",
        ),
        (
            library_source,
            'capture("QJNN_AllianceLibraryFree", frame)',
            "alliance_business_click(session, *organize.center)",
        ),
        (
            library_source,
            'capture("QJNN_AllianceLibraryReward", reward_frame)',
            "_click_reference_point(session, reward_frame, LIBRARY_REWARD_DISMISS_POINT)",
        ),
    )
    for source, capture_call, click_call in capture_before_click_pairs:
        capture_index = source.find(capture_call)
        click_index = source.find(click_call, max(0, capture_index))
        if capture_index < 0 or click_index < 0 or capture_index >= click_index:
            errors.append(f"联盟采集时序错误：{capture_call} 必须发生在原 action 前")

    arena_path = PIPELINE / "86_arena.json"
    try:
        arena_pipeline = load_json(arena_path)
        check_pipeline_references(arena_pipeline, set(nodes), arena_path, errors)
        arena_wrapper = arena_pipeline.get("QJNN_ArenaDaily", {})
        expected_arena_chain = ["QJNN_ArenaRun"]
        if not isinstance(arena_wrapper, dict):
            errors.append("86_arena.json: 缺少 QJNN_ArenaDaily")
        elif arena_wrapper.get("next") != expected_arena_chain:
            errors.append("86_arena.json: ArenaDaily 必须复用通用 HomeNavigateToP3")

        arena_run = arena_pipeline.get("QJNN_ArenaRun", {})
        expected_arena_run = {
            "recognition": "DirectHit",
            "action": "Custom",
            "custom_action": "qjnn_arena_daily",
            "max_hit": 1,
            "timeout": 240000,
            "on_error": ["QJNN_SafeStop"],
        }
        if not isinstance(arena_run, dict):
            errors.append("86_arena.json: 缺少 QJNN_ArenaRun")
        else:
            for field, expected in expected_arena_run.items():
                if arena_run.get(field) != expected:
                    errors.append(
                        f"86_arena.json: QJNN_ArenaRun.{field} 必须为 {expected!r}"
                    )

        arena_exit_debug = arena_pipeline.get("QJNN_ArenaExitDebug", {})
        expected_exit_debug = {
            "recognition": "DirectHit",
            "action": "Custom",
            "custom_action": "qjnn_arena_exit_debug",
            "max_hit": 1,
            "timeout": 30000,
            "on_error": ["QJNN_SafeStop"],
        }
        if not isinstance(arena_exit_debug, dict):
            errors.append("86_arena.json: 缺少 QJNN_ArenaExitDebug")
        else:
            for field, expected in expected_exit_debug.items():
                if arena_exit_debug.get(field) != expected:
                    errors.append(
                        f"86_arena.json: QJNN_ArenaExitDebug.{field} 必须为 {expected!r}"
                    )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"{arena_path.relative_to(ROOT)}: {exc}")

    arena_source = (ROOT / "agent" / "qjnn_arena.py").read_text(encoding="utf-8")
    arena_capture_source = (
        ROOT / "agent" / "qjnn_arena_capture.py"
    ).read_text(encoding="utf-8")

    def arena_tuple(name: str) -> tuple[int, int, int, int] | None:
        match = re.search(
            rf"^{name}\s*=\s*\((\d+),\s*(\d+),\s*(\d+),\s*(\d+)\)",
            arena_source,
            re.MULTILINE,
        )
        return tuple(int(value) for value in match.groups()) if match else None

    reward_card = arena_tuple("ARENA_REWARD_CARD_RECT")
    reward_safe = arena_tuple("ARENA_REWARD_SAFE_RECT")
    if reward_card is None or reward_safe is None:
        errors.append("qjnn_arena.py: 缺少静态 RewardCard/SafeRect")
    else:
        card_x, card_y, card_w, card_h = reward_card
        safe_x, safe_y, safe_w, safe_h = reward_safe
        if not (
            card_x < safe_x
            and card_y < safe_y
            and safe_x + safe_w < card_x + card_w
            and safe_y + safe_h < card_y + card_h
        ):
            errors.append("qjnn_arena.py: Reward SafeRect 必须严格位于奖励卡内部")

    arena_required_fragments = {
        "ARENA_STAGE_BUTTON_ROI = (220, 1160, 460, 300)": "真实 ArenaHome 登上舞台 ROI 校准",
        "ARENA_MAX_BATTLES = 5": "最多五轮比赛硬上限",
        "ARENA_MAX_REWARD_DISMISSES = 4": "最多四层奖励硬上限",
        "ARENA_POLL_INTERVAL_SECONDS = 0.5": "状态轮询间隔",
        "ARENA_PAGE_TIMEOUT_SECONDS = 20.0": "页面有限等待",
        "ARENA_OCR_THRESHOLD = 0.70": "统一 OCR 置信度门槛",
        "ARENA_BACK_ROI = (0, 0, 150, 150)": "竞技场左上返回按钮 ROI",
        "ARENA_BACK_THRESHOLD = 0.90": "竞技场返回模板阈值",
        '"arena" / "back_button.png"': "竞技场专用返回按钮模板",
        '"快速完成"': "快速完成 recognition gate",
        '"恭喜获得"': "奖励标题 recognition gate",
        "def arena_reward_white_ratio(frame)": "奖励白色卡片结构辅助诊断",
        "if title is None:": "恭喜获得 OCR 独立奖励判据",
        "if session.click(*target.center) is False:return None": "奖励 SafeRect 单次点击与输入回执",
        "safe=_arena_reward_target(session,fresh_items,fresh)": "奖励随机延迟后重新识别",
        "target=_arena_reward_target(session,newest_items,newest)": "奖励点击前第二帧识别",
        "session.sleep(ARENA_POLL_INTERVAL_SECONDS)": "奖励点击后等待再识别",
        'log("Arena remaining count did not decrease")': "次数未下降安全失败",
        'log(f"[Arena] remaining: {home.remaining}/5")': "每轮剩余次数日志",
        "return ArenaResult(False, None, 0, \"entry_not_found\")": "未知入口安全失败",
        "controller = context.tasker.controller": "正式 Maa Controller 帧来源",
        'capture("QJNN_ArenaHomeWaitFailure", frame)': "ArenaHome 等待失败帧采集",
        "log_arena_home_diagnostics(items, frame, log=log)": "ArenaHome OCR 明细日志",
        "def arena_back_button_score(frame)": "竞技场返回按钮图像识别",
        "def _exit_arena_from_confirmed_home(": "正式与调试入口共享退出逻辑",
        "def run_arena_exit(": "竞技场退出薄调试状态机",
        'capture("QJNN_ArenaExitBackFailure", home_frame)': "返回按钮失败帧采集",
    }
    for fragment, purpose in arena_required_fragments.items():
        if fragment not in arena_source:
            errors.append(f"qjnn_arena.py: 缺少{purpose}")
    if "title is None or arena_reward_white_ratio(frame)" in arena_source:
        errors.append("qjnn_arena.py: 明确的恭喜获得 OCR 不得被卡片结构否决")
    for loading_fragment in (
        "ARENA_LOADING_TIMEOUT_SECONDS = 20.0",
        "ARENA_LOADING_CYAN_RATIO_MIN = 0.08",
        "ARENA_LOADING_CYAN_RATIO_MAX = 0.35",
        "def find_arena_battle_surface(",
        "def find_arena_loading(",
        "handle_transient_if_present(",
        "loading_classifier=find_arena_loading",
        'loading_timeout_state="QJNN_ArenaLoadingTimeout"',
        'log("Arena reward flow reached an unknown non-loading page")',
    ):
        if loading_fragment not in arena_source:
            errors.append(
                "qjnn_arena.py: missing finite Arena loading guard: "
                + loading_fragment
            )
    arena_tree = ast.parse(arena_source)
    arena_delay_function = next(
        (node for node in arena_tree.body if isinstance(node, ast.FunctionDef) and node.name == "_wait_before_click"),
        None,
    )
    arena_delay_nodes = list(ast.walk(arena_delay_function)) if arena_delay_function is not None else []
    # Only delay sampling may use random; button coordinates remain deterministic.
    invalid_arena_random = any(
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "random"
        and (node.attr != "uniform" or node not in arena_delay_nodes)
        for node in ast.walk(arena_tree)
    )
    if "RandomClick" in arena_source or invalid_arena_random:
        errors.append("qjnn_arena.py: 奖励页禁止全屏或随机点击")
    if "while True" in arena_source and "deadline" not in arena_source:
        errors.append("qjnn_arena.py: 状态轮询必须有明确 deadline")

    expected_arena_capture_targets = (
        "QJNN_ArenaHomeActive",
        "QJNN_ArenaHomeDone",
        "QJNN_ArenaBattleQuickFinish",
        "QJNN_ArenaRewardPopup",
        "QJNN_ArenaRewardFailure",
        "QJNN_ArenaBackHome",
        "QJNN_ArenaHomeWaitFailure",
        "QJNN_ArenaLoadingTimeout",
        "QJNN_ArenaExitBackFailure",
    )
    for target in expected_arena_capture_targets:
        if target not in arena_capture_source:
            errors.append(f"qjnn_arena_capture.py: 缺少采集状态 {target}")
    arena_capture_requirements = (
        'ARENA_CAPTURE_DIR = DATA_ROOT / "images" / "screenshots" / "arena"',
        "class ArenaCaptureManager",
        "def capture_once",
        "self._attempted",
        'cv2.imencode(".png", frame)',
    )
    for symbol in arena_capture_requirements:
        if symbol not in arena_capture_source:
            errors.append(f"qjnn_arena_capture.py: 缺少一次性原图采集约束 {symbol}")
    lowered_arena_capture = arena_capture_source.lower()
    if "post_connection" in lowered_arena_capture or "adb" in lowered_arena_capture:
        errors.append("qjnn_arena_capture.py: 禁止创建第二套 ADB/Controller 连接")
    if "assets/resource/image" in lowered_arena_capture:
        errors.append("qjnn_arena_capture.py: 原始竞技场截图禁止写入模板目录")

    runner_arena_requirements = (
        '"--capture-arena"',
        "capture_arena: bool = False",
        "ArenaCaptureManager()",
        "arena_capture=arena_capture",
        '"qjnn_arena_daily", ArenaDailyAction(arena_capture)',
        '"qjnn_arena_exit_debug", ArenaExitDebugAction(arena_capture)',
    )
    for symbol in runner_arena_requirements:
        if symbol not in runner_source:
            errors.append(f"run_task.py: 缺少竞技场任务/采集约束 {symbol}")
    if 'AgentServer.custom_action("qjnn_arena_daily")(ArenaDailyAction)' not in agent_action_source:
        errors.append("qjnn_action.py: AgentServer 未注册 qjnn_arena_daily")
    if 'AgentServer.custom_action("qjnn_arena_exit_debug")(ArenaExitDebugAction)' not in agent_action_source:
        errors.append("qjnn_action.py: AgentServer 未注册 qjnn_arena_exit_debug")

    arena_sample_names = (
        "arena_home_active_real_001.png",
        "arena_home_done_real_001.png",
        "arena_battle_quick_finish_real_001.png",
        "arena_reward_popup_real_001.png",
        "arena_reward_coin_popup.png",
    )
    for arena_sample_name in arena_sample_names:
        arena_sample = SCREENSHOT_ROOT / "arena" / arena_sample_name
        if not arena_sample.is_file():
            missing_input(arena_sample, errors, f"缺少真实竞技场样本：images/screenshots/arena/{arena_sample_name}", source_only)
        elif arena_sample.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
            errors.append(f"{arena_sample_name}: PNG 二进制签名无效")

    competition_path = PIPELINE / "87_competition.json"
    try:
        competition_pipeline = load_json(competition_path)
        check_pipeline_references(competition_pipeline, set(nodes), competition_path, errors)
        competition_wrapper = competition_pipeline.get("QJNN_CompetitionDaily", {})
        expected_competition_chain = ["QJNN_CompetitionRun"]
        if not isinstance(competition_wrapper, dict):
            errors.append("87_competition.json: 缺少 QJNN_CompetitionDaily")
        elif competition_wrapper.get("next") != expected_competition_chain:
            errors.append("87_competition.json: CompetitionDaily 必须复用 HomeNavigateToP3")
        competition_run = competition_pipeline.get("QJNN_CompetitionRun", {})
        expected_competition_run = {
            "recognition": "DirectHit",
            "action": "Custom",
            "custom_action": "qjnn_competition_daily",
            "max_hit": 1,
            "timeout": 600000,
            "on_error": ["QJNN_SafeStop"],
        }
        if not isinstance(competition_run, dict):
            errors.append("87_competition.json: 缺少 QJNN_CompetitionRun")
        else:
            for field, expected in expected_competition_run.items():
                if competition_run.get(field) != expected:
                    errors.append(
                        f"87_competition.json: QJNN_CompetitionRun.{field} "
                        f"必须为 {expected!r}"
                    )
        competition_exit_debug = competition_pipeline.get(
            "QJNN_CompetitionExitDebug", {}
        )
        expected_competition_exit_debug = {
            "recognition": "DirectHit",
            "action": "Custom",
            "custom_action": "qjnn_competition_exit_debug",
            "max_hit": 1,
            "timeout": 60000,
            "on_error": ["QJNN_SafeStop"],
        }
        if not isinstance(competition_exit_debug, dict):
            errors.append("87_competition.json: 缺少 QJNN_CompetitionExitDebug")
        else:
            for field, expected in expected_competition_exit_debug.items():
                if competition_exit_debug.get(field) != expected:
                    errors.append(
                        f"87_competition.json: QJNN_CompetitionExitDebug.{field} "
                        f"必须为 {expected!r}"
                    )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"{competition_path.relative_to(ROOT)}: {exc}")

    competition_source = (
        ROOT / "agent" / "qjnn_competition.py"
    ).read_text(encoding="utf-8")
    competition_capture_source = (
        ROOT / "agent" / "qjnn_competition_capture.py"
    ).read_text(encoding="utf-8")
    competition_required_fragments = {
        "COMPETITION_HOME_THEME_ROI = (120, 0, 660, 520)": "真实 CompetitionHome 主题 ROI 校准",
        "COMPETITION_REMAINING_ROI = (450, 1480, 430, 120)": "真实 JudgePage 次数 ROI 校准",
        "COMPETITION_LEFT_PLAYER_NAME_ROI = (100, 1280, 250, 120)": "左玩家名 ROI",
        "COMPETITION_RIGHT_PLAYER_NAME_ROI = (550, 1280, 250, 120)": "右玩家名 ROI",
        "COMPETITION_PAIR_STABLE_FRAMES = 2": "单侧玩家名连续两帧防抖",
        "NEXT_VOTE_DEBOUNCE_MIN_SECONDS = 1.0": "下一票随机防抖下限 1.0 秒",
        "NEXT_VOTE_DEBOUNCE_MAX_SECONDS = 1.5": "下一票随机防抖上限 1.5 秒",
        "SUCCESSFUL_VOTES_TARGET = 20": "成功点赞目标 20",
        "MAX_VOTE_ATTEMPTS = 20": "最多点击尝试 20",
        "for _ in range(max(0, max_vote_attempts - vote_attempts))": "接续后仍受总点击上限保护",
        "if judge.remaining == 0:\n            exhausted = True": "0/30 禁止点赞并正常退出",
        "choices = (judge.left_like, judge.right_like)": "随机候选仅限两个赞按钮",
        "selected = choose_like(choices)": "可注入 RNG",
        "if selected not in choices": "RNG 越界保护",
        "def find_vote_refresh_state(": "投票刷新 ready gate",
        'pattern = re.compile(r"次数:?([0-9]{1,2})/30")': "带次数前缀且允许倒计时后缀的 remaining parser",
        "class CompetitionVoteRefreshTracker": "分阶段 VoteRefresh 状态机",
        "def _debounce_and_revalidate_vote(": "点赞前防抖重验",
        "random.uniform(": "每票使用随机防抖等待",
        "session.sleep(delay)": "随机防抖等待后再重验",
        "current = find_judge_page(items, frame)": "防抖后重新识别 JudgePage",
        "current.remaining > 0": "防抖后确认次数可用，允许自然恢复",
        'self.phase = "waiting_pair_change"': "等待 player pair 变化阶段",
        'self.phase = "waiting_pair_stable"': "等待 player pair 稳定阶段",
        "left_changed_confirmed or right_changed_confirmed": "任意一侧可靠变化即可确认换组",
        "stable_count >= COMPETITION_PAIR_STABLE_FRAMES": "单侧变化至少连续两帧",
        "previous.left_player_name is None\n            and previous.right_player_name is None": "至少一侧 old identity 才能点赞",
        '".。,，·•:：;；\'\\"“”‘’|丨_—-~～[]【】()（）<>《》"': "玩家名首尾伪标点归一化",
        "successful_votes += 1": "仅确认换组后累计成功票数",
        "if successful_votes >= successful_votes_target": "达到本次运行目标立即停止点赞",
        'f"[Competition] remaining: 0/30; "': "0/30 合法终态正常退出日志",
        "def log_vote_refresh_diagnostics(": "VoteRefresh OCR/轮询明细诊断",
        "refresh_observations": "VoteRefresh 每次轮询结果留档",
        '"QJNN_CompetitionNextPairWaitFailure"': "下一组选手等待失败采集",
        '"本期搭配主题"': "评选赛首页结构 gate",
        '"我当评委"': "评委入口 gate",
        "if left is None or right is None": "双赞同时确认 gate",
        "COMPETITION_JUDGE_BACK_TEMPLATE": "评委页专用返回模板槽",
        "COMPETITION_HOME_BACK_TEMPLATE": "评选赛首页专用返回模板槽",
        "judge_back_score = session.judge_back_score(judge_frame)": "评委页返回图像 gate",
        "home_back_score = session.home_back_score(home_frame)": "首页返回图像 gate",
        "def run_competition_judge_exit(": "可复用的正式 JudgePage 退出流程",
        "def run_competition_home_exit(": "可复用的正式 CompetitionHome 退出流程",
        "def run_competition_exit_debug(": "只复用正式退出流程的调试入口",
        "vote_attempts += 1": "点击尝试次数仅在实际点赞后增加",
        "False, judge.remaining, successful_votes, reason": "点击后换组失败立即停止且不补点",
        "controller = context.tasker.controller": "正式 Maa Controller 帧来源",
        "def log_competition_home_diagnostics(": "CompetitionHome OCR 明细诊断",
        "log_competition_home_diagnostics(items, frame, log=log)": "仅 Capture 模式超时诊断",
        "def log_competition_judge_diagnostics(": "JudgePage OCR 明细诊断",
        "log_competition_judge_diagnostics(items, frame, log=log)": "JudgePage 超时诊断",
    }
    for fragment, purpose in competition_required_fragments.items():
        if fragment not in competition_source:
            errors.append(f"qjnn_competition.py: 缺少{purpose}")
    for forbidden_fragment in (
        "COMPETITION_DAILY_VOTE_CAPACITY",
        "votes_needed_for_daily_target",
        "daily_vote_capacity",
        "_wait_for_recovered_votes",
    ):
        if forbidden_fragment in competition_source:
            errors.append(
                "qjnn_competition.py: forbidden cross-run daily accounting/recovery: "
                + forbidden_fragment
            )
    if "while True" in competition_source:
        errors.append("qjnn_competition.py: 禁止无上限 while True")
    if "(?![0-9])" in competition_source:
        errors.append("qjnn_competition.py: remaining parser 不得拒绝 /30 后紧邻倒计时")
    if "RandomClick" in competition_source or "random.randint" in competition_source:
        errors.append("qjnn_competition.py: 禁止全屏或坐标随机点击")
    for obsolete_constraint in (
        "unexpected_candidate",
        "unexpected_stable_count",
        "invalid_remaining",
        "Competition remaining count did not decrease",
        "judge.remaining != 0",
    ):
        if obsolete_constraint in competition_source:
            errors.append(
                f"qjnn_competition.py: 禁止保留旧 remaining 业务约束 {obsolete_constraint}"
            )
    run_daily_position = competition_source.find("def run_competition_daily(")
    debounce_call = "revalidated_vote = _debounce_and_revalidate_vote("
    debounce_position = competition_source.find(debounce_call, run_daily_position)
    selection_position = competition_source.find(
        "selected = choose_like(choices)", debounce_position
    )
    click_position = competition_source.find(
        "session.click(*selected.center)", selection_position
    )
    if not (0 <= debounce_position < selection_position < click_position):
        errors.append("qjnn_competition.py: 防抖重验必须发生在随机选择和点赞之前")
    debounce_helper = re.search(
        r"def _debounce_and_revalidate_vote\(.*?(?=\n\ndef )",
        competition_source,
        flags=re.DOTALL,
    )
    if debounce_helper is None or not all(
        fragment in debounce_helper.group(0)
        for fragment in (
            "delay_provider()",
            "session.sleep(delay)",
            "session.capture_frame()",
            "find_judge_page(items, frame)",
            "current.remaining > 0",
        )
    ):
        errors.append("qjnn_competition.py: debounce 后必须抓取新帧并完整 revalidate")
    if debounce_helper is not None:
        helper_tree = ast.parse(debounce_helper.group(0))
        for comparison in ast.walk(helper_tree):
            if not isinstance(comparison, ast.Compare):
                continue
            operands = [comparison.left, *comparison.comparators]
            if any(
                isinstance(operator, ast.Eq)
                and isinstance(left, ast.Attribute) and left.attr == "remaining"
                and isinstance(right, ast.Attribute) and right.attr == "remaining"
                for left, operator, right in zip(operands, comparison.ops, operands[1:])
            ):
                errors.append("qjnn_competition.py: VoteReady 不得要求 remaining 与首帧相等")
    if competition_source.count("session.click(*selected.center)") != 1:
        errors.append("qjnn_competition.py: 每轮只能存在一次受 gate 保护的点赞")
    refreshed_position = competition_source.find("if refreshed is None:", click_position)
    success_increment_position = competition_source.find(
        "successful_votes += 1", refreshed_position
    )
    target_break_position = competition_source.find(
        "if successful_votes >= successful_votes_target", success_increment_position
    )
    if not (
        0
        <= click_position
        < refreshed_position
        < success_increment_position
        < target_break_position
    ):
        errors.append(
            "qjnn_competition.py: 只有换组确认后才能累计，达到20票后必须先于下一次点击退出"
        )
    vote_attempt_position = competition_source.find("vote_attempts += 1", selection_position)
    pending_save_position = competition_source.find("save_progress()", vote_attempt_position)
    if not (selection_position < vote_attempt_position < pending_save_position < click_position):
        errors.append(
            "qjnn_competition.py: 点赞前必须记录尝试和待确认状态，避免中断后重复提交"
        )
    judge_exit_block = re.search(
        r"def run_competition_judge_exit\(.*?(?=\n\ndef )",
        competition_source,
        flags=re.DOTALL,
    )
    if judge_exit_block is None or not all(
        fragment in judge_exit_block.group(0)
        for fragment in (
            "successful_votes < required_successful_votes",
            "find_judge_page(judge_items, judge_frame)",
            "session.judge_back_score(judge_frame)",
            "run_competition_home_exit(",
        )
    ):
        errors.append(
            "qjnn_competition.py: JudgeExit 必须同时受成功票数、JudgePage 与专用模板 gate 保护"
        )
    home_exit_block = re.search(
        r"def run_competition_home_exit\(.*?(?=\n\ndef )",
        competition_source,
        flags=re.DOTALL,
    )
    if home_exit_block is None or not all(
        fragment in home_exit_block.group(0)
        for fragment in (
            "find_competition_home(home_items, home_frame)",
            "session.home_back_score(home_frame)",
            "COMPETITION_BACK_CLICK_POINT",
        )
    ):
        errors.append(
            "qjnn_competition.py: CompetitionHome Exit 必须同时受页面与专用模板 gate 保护"
        )
    exit_debug_block = re.search(
        r"def run_competition_exit_debug\(.*?(?=\n\ndef )",
        competition_source,
        flags=re.DOTALL,
    )
    if (
        exit_debug_block is None
        or "run_competition_judge_exit(" not in exit_debug_block.group(0)
        or "choose_like" in exit_debug_block.group(0)
        or "selected.center" in exit_debug_block.group(0)
    ):
        errors.append(
            "qjnn_competition.py: ExitDebug 必须复用正式两级退出且不得包含点赞动作"
        )
    if "if left_player_name is None or right_player_name is None" in competition_source:
        errors.append("qjnn_competition.py: 下一组选手 gate 不得强制左右玩家名同时存在")
    vote_timeout_block = re.search(
        r"def vote_refresh_timeout\(.*?(?=\n        refreshed = _poll_snapshot)",
        competition_source,
        flags=re.DOTALL,
    )
    if (
        vote_timeout_block is None
        or "log_vote_refresh_diagnostics(" not in vote_timeout_block.group(0)
    ):
        errors.append("qjnn_competition.py: VoteRefresh 超时路径必须实际调用诊断函数")
    if "player_pair" in competition_capture_source:
        errors.append("qjnn_competition_capture.py: 禁止持久化玩家名或 pair")
    for persistence_call in ("json.dump", "write_text", "write_bytes"):
        if persistence_call in competition_source:
            errors.append(
                f"qjnn_competition.py: 玩家 pair 仅允许运行时内存使用，禁止 {persistence_call}"
            )

    expected_competition_capture_targets = (
        "QJNN_CompetitionHome",
        "QJNN_CompetitionJudgeActive",
        "QJNN_CompetitionJudgeDone",
        "QJNN_CompetitionBackHome",
        "QJNN_CompetitionHomeWaitFailure",
        "QJNN_CompetitionJudgeWaitFailure",
        "QJNN_CompetitionNextPairWaitFailure",
        "QJNN_CompetitionJudgeBackFailure",
        "QJNN_CompetitionHomeBackFailure",
    )
    for target in expected_competition_capture_targets:
        if target not in competition_capture_source:
            errors.append(f"qjnn_competition_capture.py: 缺少采集状态 {target}")
    competition_capture_requirements = (
        'COMPETITION_CAPTURE_DIR = DATA_ROOT / "images" / "screenshots" / "competition"',
        "class CompetitionCaptureManager",
        "def capture_once",
        "self._attempted",
        'cv2.imencode(".png", frame)',
    )
    for symbol in competition_capture_requirements:
        if symbol not in competition_capture_source:
            errors.append(f"qjnn_competition_capture.py: 缺少一次性原图采集约束 {symbol}")
    lowered_competition_capture = competition_capture_source.lower()
    if "post_connection" in lowered_competition_capture or "adb" in lowered_competition_capture:
        errors.append("qjnn_competition_capture.py: 禁止创建第二套 ADB/Controller 连接")
    if "assets/resource/image" in lowered_competition_capture:
        errors.append("qjnn_competition_capture.py: 原始评选赛截图禁止写入模板目录")

    runner_competition_requirements = (
        '"--capture-competition"',
        "capture_competition: bool = False",
        "CompetitionCaptureManager()",
        "competition_capture=competition_capture",
        '"qjnn_competition_daily", CompetitionDailyAction(competition_capture, run_state=run_state)',
        '"qjnn_competition_exit_debug"',
        "CompetitionExitDebugAction(competition_capture)",
    )
    for symbol in runner_competition_requirements:
        if symbol not in runner_source:
            errors.append(f"run_task.py: 缺少评选赛任务/采集约束 {symbol}")
    if (
        'AgentServer.custom_action("qjnn_competition_daily")(CompetitionDailyAction)'
        not in agent_action_source
    ):
        errors.append("qjnn_action.py: AgentServer 未注册 qjnn_competition_daily")
    if (
        'AgentServer.custom_action("qjnn_competition_exit_debug")'
        not in agent_action_source
    ):
        errors.append(
            "qjnn_action.py: AgentServer 未注册 qjnn_competition_exit_debug"
        )

    for template_name in ("judge_back_button.png", "home_back_button.png"):
        template_path = TEMPLATE_ROOT / "competition" / template_name
        if not template_path.is_file():
            errors.append(f"缺少 Competition 专用返回模板：{template_path.relative_to(ROOT)}")
        elif template_path.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
            errors.append(f"{template_name}: PNG 二进制签名无效")
    for sample_name in (
        "competition_judge_done_real_001.png",
        "competition_judge_done_real_002.png",
    ):
        sample_path = SCREENSHOT_ROOT / "competition" / sample_name
        if not sample_path.is_file():
            missing_input(sample_path, errors, f"缺少 JudgeBack 跨样本验证图：{sample_name}", source_only)
        elif sample_path.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
            errors.append(f"{sample_name}: PNG 二进制签名无效")

    common_popup_path = ROOT / "agent" / "qjnn_common_popup.py"
    common_popup_capture_path = ROOT / "agent" / "qjnn_common_popup_capture.py"
    try:
        common_popup_source = common_popup_path.read_text(encoding="utf-8")
        common_popup_capture_source = common_popup_capture_path.read_text(
            encoding="utf-8"
        )
    except OSError as exc:
        errors.append(f"公共安全弹窗模块缺失：{exc}")
        common_popup_source = ""
        common_popup_capture_source = ""
    common_popup_requirements = {
        "MAX_COMMON_POPUP_DISMISS = 5": "公共弹窗五层硬上限",
        "COMMON_ACHIEVEMENT_POPUP_RECT = (80, 230, 560, 760)": "成就弹窗主体区域",
        "COMMON_ACHIEVEMENT_SAFE_RECT = (120, 570, 200, 45)": "成就弹窗安全点击区域",
        "COMMON_ACHIEVEMENT_SHARE_FORBIDDEN_RECT = (500, 630, 170, 100)": "真实分享按钮禁区",
        "COMMON_POPUP_DISAPPEAR_TIMEOUT_SECONDS = 5.0": "弹窗消失等待上限",
        "COMMON_POPUP_POLL_INTERVAL_SECONDS = 0.4": "弹窗消失轮询间隔",
        'title = _find_ocr(items, "新成就达成", title_roi)': "成就标题白名单 OCR",
        "if title is not None:": "标题独立白名单判据",
        "class GlobalTransientResult(str, Enum):": "统一临时状态结果",
        "def handle_transient_if_present(": "业务失败后临时状态处理接口",
        "if self.dismiss_count >= self.dismiss_limit(detection)": "dismiss 上限检查",
        "click(*click_point)": "每层仅在 SafeRect 内点击一次",
        "COMMON_SKILL_UNLOCK_POPUP_RECT = (30, 265, 660, 575)": "技能弹窗主体区域",
        "COMMON_SKILL_UNLOCK_SAFE_RECT = (240, 870, 240, 120)": "技能弹框外安全区域",
        'items, "学会新技能"': "技能弹窗标题白名单",
        "self.dismiss_count += 1": "明确弹窗关闭计数",
        "value = selector(last_items, last_frame)": "业务识别优先",
        "transient, fresh_frame, fresh_items = handle_transient_if_present(": "业务 miss 后临时状态 fallback",
        "if rects_intersect(safe, detection.share_box)": "实际分享 OCR 框重叠保护",
        "def run_common_popup_debug(": "公共弹窗薄调试入口",
        "log_common_popup_diagnostics(detection, frame": "点击前识别诊断",
        "def render_common_popup_click_map(": "调试点击坐标标注图",
        'capture_state + "ClickMap"': "调试点击坐标图采集",
        'click_map_saved = self.capture(': "每次成就点击前保存坐标图",
        'controller.post_click={click_point}': "实际 Controller 点击坐标标注",
        "def device_display_info() -> str:": "设备物理尺寸只读诊断",
        '[adb_path, "-s", serial, "shell", "wm", "size"]': "wm size 只读查询",
        "class CommonPopupDebugAction(CustomAction)": "Maa 公共弹窗调试 Action",
        "class CommonPopupPreflightAction(CustomAction)": "公共弹窗全局前置 Action",
        "class CommonPopupRuntimeAction(CustomAction)": "公共弹窗运行期 Action",
        "captures stay raw; business code owns fallback timing": "CustomAction 原始截图不再前置拦截",
        "def monotonic() -> float:": "运行时有限轮询单调时钟",
        "def sleep(seconds: float) -> None:": "运行时有限轮询等待适配",
        "def wait_for_popup_disappear(": "有限弹窗消失轮询",
        "last_frame = session.capture_frame()": "每轮重新获取正式 Controller 新帧",
        '"QJNN_CommonAchievementPopupDismissFailure"': "弹窗未消失失败采集",
    }
    for fragment, purpose in common_popup_requirements.items():
        if fragment not in common_popup_source:
            errors.append(f"qjnn_common_popup.py: 缺少{purpose}")
    popup_rect = (80, 230, 560, 760)
    popup_safe_rect = (120, 570, 200, 45)
    popup_share_forbidden = (500, 630, 170, 100)
    if not rect_contains(list(popup_rect), list(popup_safe_rect)):
        errors.append("qjnn_common_popup.py: SafeRect 必须完全位于 PopupRect 内")
    if rects_intersect(list(popup_safe_rect), list(popup_share_forbidden)):
        errors.append("qjnn_common_popup.py: SafeRect 禁止与 ShareForbiddenRect 相交")
    if "RandomClick" in common_popup_source:
        errors.append("qjnn_common_popup.py: 公共弹窗禁止全屏随机点击")
    if (common_popup_source.count("random.randrange(") != 2
            or "if detection.kind is CommonPopupKind.SKILL_UNLOCK" not in common_popup_source):
        errors.append("qjnn_common_popup.py: 随机点击必须限定于技能弹窗 SafeRect")
    if rects_intersect([30, 265, 660, 575], [240, 870, 240, 120]):
        errors.append("qjnn_common_popup.py: 技能弹窗 SafeRect 必须位于框外")
    if common_popup_source.count("click(*click_point)") != 1:
        errors.append("qjnn_common_popup.py: 每层公共弹窗只能存在一次 SafeRect 点击")
    poll_block = re.search(
        r"def poll_target_with_common_popups\(.*$",
        common_popup_source,
        flags=re.DOTALL,
    )
    if poll_block is None:
        errors.append("qjnn_common_popup.py: 缺少业务优先 transient 等待器")
    else:
        poll_text = poll_block.group(0)
        capture_position = poll_text.find("last_frame = session.capture_frame()")
        popup_position = poll_text.find("handle_transient_if_present(")
        target_position = poll_text.find("value = selector(")
        if not (0 <= capture_position < target_position < popup_position):
            errors.append(
                "qjnn_common_popup.py: 必须先判断业务目标，miss 后才处理 transient"
            )
    if "poll_target_with_common_popups(" not in competition_source:
        errors.append("qjnn_competition.py: 最终页面等待尚未接入业务优先 transient 等待器")
    common_capture_requirements = (
        'COMMON_POPUP_CAPTURE_DIR = DATA_ROOT / "images" / "screenshots" / "common_popup"',
        '"QJNN_CommonAchievementPopup"',
        '"QJNN_CommonAchievementPopupClickMap"',
        '"QJNN_CommonAchievementPopupDismissFailure"',
        '"QJNN_CommonPopupUnknown"',
        "COMMON_POPUP_REPEATABLE_CAPTURE_STATES",
        "self._attempted",
        'cv2.imencode(".png", frame)',
    )
    for fragment in common_capture_requirements:
        if fragment not in common_popup_capture_source:
            errors.append(f"qjnn_common_popup_capture.py: 缺少采集约束 {fragment}")
    for fragment in (
        '"--capture-common-popup"',
        "capture_common_popup: bool = False",
        "CommonPopupCaptureManager()",
        "configure_common_popup_capture(",
        '"qjnn_common_popup_debug"',
        "CommonPopupDebugAction(common_popup_capture)",
        '"qjnn_common_popup_preflight"',
        "CommonPopupPreflightAction(common_popup_capture)",
        'tasker.post_task("QJNN_CommonPopupPreflight")',
        "should_run_common_popup_preflight(task_name)",
        '"qjnn_common_popup_runtime"',
        "CommonPopupRuntimeAction()",
        "build_common_popup_runtime_override()",
        'COMMON_POPUP_RUNTIME_JUMPBACK = "[JumpBack]QJNN_CommonPopupRuntime"',
    ):
        if fragment not in runner_source:
            errors.append(f"run_task.py: 缺少公共弹窗采集入口 {fragment}")
    disappear_block = re.search(
        r"def wait_for_popup_disappear\(.*?(?=\n\ndef )",
        common_popup_source,
        flags=re.DOTALL,
    )
    if disappear_block is None:
        errors.append("qjnn_common_popup.py: 缺少弹窗消失等待状态机")
    else:
        disappear_text = disappear_block.group(0)
        sleep_position = disappear_text.find("session.sleep(")
        fresh_capture_position = disappear_text.find("session.capture_frame()")
        recognition_position = disappear_text.find(
            "detect_common_safe_popup(last_frame, items)"
        )
        if not (
            0 <= sleep_position < fresh_capture_position < recognition_position
        ):
            errors.append(
                "qjnn_common_popup.py: 点击后每轮必须等待、重新截图并重新识别，禁止复用旧帧"
            )
        if "session.click(" in disappear_text or "inspect_and_handle(" in disappear_text:
            errors.append("qjnn_common_popup.py: 弹窗消失等待阶段禁止任何补点")

    real_common_popup_sample = (
        SCREENSHOT_ROOT / "common_popup" / "common_achievement_popup_real_001.png"
    )
    if not real_common_popup_sample.is_file():
        missing_input(real_common_popup_sample, errors,
            "缺少真实公共成就弹窗样本："
            "images/screenshots/common_popup/common_achievement_popup_real_001.png",
            source_only)
    elif real_common_popup_sample.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
        errors.append("common_achievement_popup_real_001.png: PNG 二进制签名无效")
    if (
        'AgentServer.custom_action("qjnn_common_popup_debug")(CommonPopupDebugAction)'
        not in agent_action_source
    ):
        errors.append("qjnn_action.py: AgentServer 未注册 qjnn_common_popup_debug")
    if 'AgentServer.custom_action("qjnn_common_popup_preflight")' not in agent_action_source:
        errors.append("qjnn_action.py: missing qjnn_common_popup_preflight registration")
    if 'AgentServer.custom_action("qjnn_common_popup_runtime")' not in agent_action_source:
        errors.append("qjnn_action.py: missing qjnn_common_popup_runtime registration")
    runtime_guarded_sources = (
        (home_navigation_source, "handle_transient_if_present", "home navigation"),
        (alliance_source, "poll_target_with_common_popups", "alliance commission"),
        (library_source, "handle_transient_if_present", "library"),
        (alliance_daily_source, "handle_transient_if_present", "alliance daily"),
        (arena_source, "handle_transient_if_present", "arena"),
        (competition_source, "poll_target_with_common_popups", "competition"),
        (friend_source, "handle_transient_if_present", "friend send heart"),
    )
    for source, fragment, label in runtime_guarded_sources:
        if fragment not in source:
            errors.append(f"{label}: missing business-first transient fallback")

    common_popup_pipeline_path = PIPELINE / "88_common_popup.json"
    try:
        common_popup_pipeline = load_json(common_popup_pipeline_path)
        common_debug_node = common_popup_pipeline.get("QJNN_CommonPopupDebug", {})
        expected_common_debug = {
            "recognition": "DirectHit",
            "action": "Custom",
            "custom_action": "qjnn_common_popup_debug",
            "max_hit": 1,
            "timeout": 30000,
            "on_error": ["QJNN_SafeStop"],
        }
        for field, expected in expected_common_debug.items():
            if common_debug_node.get(field) != expected:
                errors.append(
                    f"88_common_popup.json: QJNN_CommonPopupDebug.{field} "
                    f"必须为 {expected!r}"
                )
        common_preflight_node = common_popup_pipeline.get(
            "QJNN_CommonPopupPreflight", {}
        )
        expected_common_preflight = {
            "recognition": "DirectHit",
            "action": "Custom",
            "custom_action": "qjnn_common_popup_preflight",
            "max_hit": 1,
            "timeout": 30000,
            "on_error": ["QJNN_SafeStop"],
        }
        for field, expected in expected_common_preflight.items():
            if common_preflight_node.get(field) != expected:
                errors.append(
                    f"88_common_popup.json: QJNN_CommonPopupPreflight.{field} "
                    f"must be {expected!r}"
                )
        common_runtime_node = common_popup_pipeline.get(
            "QJNN_CommonPopupRuntime", {}
        )
        expected_common_runtime = {
            "recognition": "OCR",
            "expected": ["新成就达成", "学会新技能"],
            "roi": [90, 220, 540, 260],
            "threshold": 0.7,
            "action": "Custom",
            "custom_action": "qjnn_common_popup_runtime",
            "on_error": ["QJNN_SafeStop"],
        }
        for field, expected in expected_common_runtime.items():
            if common_runtime_node.get(field) != expected:
                errors.append(
                    f"88_common_popup.json: QJNN_CommonPopupRuntime.{field} "
                    f"must be {expected!r}"
                )
        if common_runtime_node.get("next"):
            errors.append(
                "88_common_popup.json: runtime popup handler must end for JumpBack"
            )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"{common_popup_pipeline_path.relative_to(ROOT)}: {exc}")

    check_runtime_submission(runner_source,
        (ROOT / "agent/qjnn_back_to_home.py").read_text(encoding="utf-8"), errors)
    exempt_start = runner_source.find("COMMON_POPUP_PREFLIGHT_EXEMPT_TASKS")
    exempt_end = runner_source.find("def should_run_common_popup_preflight")
    exempt_block = runner_source[exempt_start:exempt_end]
    for formal_entry in (
        "QJNN_StartAndSignin",
        "QJNN_Mail",
        "QJNN_AllianceDaily",
        "QJNN_FriendSendHeart",
    ):
        if f'"{formal_entry}",' in exempt_block:
            errors.append(
                f"run_task.py: formal task {formal_entry} must not bypass CommonPopup preflight"
            )
    if '"QJNN_CommonPopupRuntime"' not in runner_source[
        runner_source.find("COMMON_POPUP_RUNTIME_EXEMPT_NODES") :
        runner_source.find("def should_run_common_popup_preflight")
    ]:
        errors.append("run_task.py: runtime popup node must be exempt from its own guard")

    competition_home_sample = (
        SCREENSHOT_ROOT / "competition" / "competition_home_real_001.png"
    )
    if not competition_home_sample.is_file():
        missing_input(competition_home_sample, errors,
            "缺少真实 CompetitionHome 样本："
            "images/screenshots/competition/competition_home_real_001.png",
            source_only)
    elif competition_home_sample.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
        errors.append("competition_home_real_001.png: PNG 二进制签名无效")

    competition_judge_sample = (
        SCREENSHOT_ROOT / "competition" / "competition_judge_active_real_001.png"
    )
    if not competition_judge_sample.is_file():
        missing_input(competition_judge_sample, errors,
            "缺少真实 JudgePage 样本："
            "images/screenshots/competition/competition_judge_active_real_001.png",
            source_only)
    elif competition_judge_sample.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
        errors.append("competition_judge_active_real_001.png: PNG 二进制签名无效")

    competition_refresh_sample = (
        SCREENSHOT_ROOT / "competition" / "competition_judge_active_real_003.png"
    )
    if not competition_refresh_sample.is_file():
        missing_input(competition_refresh_sample, errors,
            "缺少真实 29/30 VoteRefresh 样本："
            "images/screenshots/competition/competition_judge_active_real_003.png",
            source_only)
    elif competition_refresh_sample.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
        errors.append("competition_judge_active_real_003.png: PNG 二进制签名无效")

    competition_single_side_sample = (
        SCREENSHOT_ROOT
        / "competition"
        / "competition_next_pair_wait_failure_real_001.png"
    )
    if not competition_single_side_sample.is_file():
        missing_input(competition_single_side_sample, errors,
            "缺少真实单侧玩家名换组样本："
            "images/screenshots/competition/"
            "competition_next_pair_wait_failure_real_001.png",
            source_only)
    elif competition_single_side_sample.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
        errors.append(
            "competition_next_pair_wait_failure_real_001.png: PNG 二进制签名无效"
        )

    state_recovery_path = ROOT / "agent" / "qjnn_state_recovery.py"
    try:
        state_recovery_source = state_recovery_path.read_text(encoding="utf-8")
        for fragment in (
            "class TaskStateSnapshot",
            "def detect_entry_state(",
            "state = detector(items, frame)",
            "handle_transient_if_present(",
            "state=UNKNOWN; zero clicks",
        ):
            if fragment not in state_recovery_source:
                errors.append(
                    f"qjnn_state_recovery.py: missing recovery contract {fragment}"
                )
        if state_recovery_source.find("state = detector(items, frame)") > state_recovery_source.find(
            "transient, fresh_frame, fresh_items = handle_transient_if_present("
        ):
            errors.append(
                "qjnn_state_recovery.py: business state detection must precede transient fallback"
            )
    except OSError as exc:
        errors.append(f"missing state recovery helper: {exc}")

    if errors:
        for error in errors:
            print(f"[ERROR] {error}")
        return 1

    print(f"[OK] interface.json 有效；读取 {len(nodes)} 个 Pipeline 节点。")
    print("[OK] UI 任务入口、JSON 配置、模板引用和 Python 语法检查通过。")
    print("[OK] Python 源码中未发现默认首设备选择或硬编码本地 ADB 端口。")
    print("[OK] ADB alias 先按 android_id 与设备身份保守合并，再统计目标 package 实例。")
    print("[OK] 模板、测试截图和 debug 分类目录结构有效。")
    print("[OK] 启动并签到入口、jump_back 串联、任务引用和无回环检查通过。")
    print("[OK] 启动 Pipeline 不含空、匿名、null 或不存在的节点引用。")
    print("[OK] Pipeline inverse 均为顶层执行；复合 recognition 无 inverse 继承风险。")
    print("[OK] Pipeline CustomRecognition/CustomAction 均覆盖正式 runtime 注册集合。")
    print(
        "[OK] 启动通过双帧确认 HOME P1/P2/P3，不再等待签到入口或依赖任务文字。"
    )
    print(
        "[OK] 签到 RewardFlow 仅在明确奖励状态下单击；服装安全区与分享禁区隔离，"
        "总 dismiss 上限为 13。"
    )
    print("[OK] 启动任务先识别已有页面和前台包，仅在游戏不在前台时执行一次 StartApp。")
    print("[OK] 第三方登录隐私等待仅使用内存模板匹配，并在首帧前关闭错误截图。")
    print("[OK] 系统邮件任务使用 OCR、相对红点 ColorMatch 和有限点击；不处理好友邮件或公告。")
    print("[OK] 系统邮件入口按邮箱页、首页顺序分流；三条正常路径完整且无循环。")
    print("[OK] 通用主页导航共享 P1/P2/P3 状态机；逐次确认且最多翻页两次。")
    print("[OK] 联盟委托入口、0/5 安全分支、完成1次保护和循环上限配置有效。")
    print("[OK] 联盟结算使用 20 秒有限轮询；未知帧不点击，调试入口复用正式状态机。")
    print("[OK] 联盟后半段调试仅从明确 0/5 地图安全返回，并复用 Library 与统一退出节点。")
    print("[OK] 图书馆入口、免费完成付费保护、奖励确认和返回验证配置有效。")
    print("[OK] 联盟日常先复用通用导航到 P3，再按委托、图书馆、统一退出编排。")
    print("[OK] 联盟素材采集默认关闭；六个目标均在原 action 前一次性保存到截图目录。")
    print(
        "[OK] 竞技场日常独立复用 Home P3 导航；比赛/奖励循环有硬上限，"
        "奖励仅点击卡片内 SafeRect，采集默认关闭。"
    )
    print(
        "[OK] 竞技场退出同时要求 ArenaHome 0/5 与左上专用模板；"
        "ExitDebug 复用正式退出函数，失败诊断不盲点。"
    )
    print(
        "[OK] 搭配评选赛独立复用 Home P3 导航；玩家换组确认成功票、"
        "20 个确认成功票目标、20 次点击尝试上限、次数恢复等待、两级返回 gate 与一次性采集约束有效。"
    )
    print(
        "[OK] 公共安全弹窗采用标题或确认信息卡识别、Popup/Safe/ShareForbidden 几何约束、"
        "五层硬上限与 business-first fallback 重新识别；未知弹窗零点击停止。"
    )
    print(
        "[OK] 好友送心从任意 HOME/好友页分流；OCR box 单击一次，"
        "结果阶段有限轮询且零补点，返回接受 P1/P2/P3。"
    )
    for filename, status in MAIL_SAMPLE_FILES.items():
        print(f"[INFO] 邮箱样本 {filename}: {status}")
    for sample_status in invalid_alliance_samples:
        print(f"[INFO] INVALID ALLIANCE SAMPLE: {sample_status}")
    for template in sorted(missing_templates):
        print(f"[INFO] MISSING TEMPLATE: assets/resource/image/{template}")
    missing_ocr_files = [
        name for name in OCR_MODEL_FILES if not (OCR_MODEL_ROOT / name).is_file()
    ]
    maa_binding_available = importlib.util.find_spec("maa") is not None
    if maa_binding_available and not missing_ocr_files:
        print("[OK] MaaFramework OCR binding 与模型文件已就绪。")
    else:
        reasons: list[str] = []
        if not maa_binding_available:
            reasons.append("当前 Python 环境缺少 maa binding")
        if missing_ocr_files:
            reasons.append("缺少模型文件 " + ", ".join(missing_ocr_files))
        print("[INFO] OCR NOT READY: " + "；".join(reasons))
        print("[INFO] 签到流程继续使用已验证的 TemplateMatch，不启用未验证 OCR。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

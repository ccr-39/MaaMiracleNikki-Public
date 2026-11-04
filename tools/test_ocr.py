from __future__ import annotations

import argparse
import sys
from pathlib import Path
from offline_test_support import PRIVATE_ROOTS, require_file


ROOT = Path(__file__).resolve().parent.parent
OCR_MODEL_ROOT = ROOT / "assets" / "resource" / "model" / "ocr"
OCR_MODEL_FILES = ("det.onnx", "keys.txt", "rec.onnx")
TARGET_TEXTS = ("签到", "暖暖月签到", "点击领取")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run MaaFramework OCR against one local image without a device or Pipeline."
    )
    parser.add_argument("image", type=Path, help="Local PNG/JPG/JPEG image path")
    return parser


def load_image(path: Path):
    path = Path(path)
    if any(path.resolve().is_relative_to(base) for base in PRIVATE_ROOTS):
        require_file(path)
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("缺少 OpenCV；请先安装 agent/requirements.txt") from exc

    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError as exc:
        raise RuntimeError(f"无法读取图片：{path}（{exc}）") from exc
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"图片格式无效或无法解码：{path}")
    return image


def run_ocr(image_path: Path):
    require_file(image_path)
    try:
        from maa.library import Library
        from maa.pipeline import JOCR, JRecognitionType
        from maa.resource import Resource
        from maa.tasker import Tasker
    except ImportError as exc:
        raise RuntimeError(
            "缺少 MaaFramework Python binding；请安装 agent/requirements.txt"
        ) from exc

    missing = [name for name in OCR_MODEL_FILES if not (OCR_MODEL_ROOT / name).is_file()]
    if missing:
        from unittest import SkipTest
        raise SkipTest(
            "缺少 OCR 模型文件：" + ", ".join(str(OCR_MODEL_ROOT / name) for name in missing)
        )

    image = load_image(image_path)
    resource = Resource()
    model_job = resource.post_ocr_model(OCR_MODEL_ROOT).wait()
    if not model_job.succeeded or not resource.loaded:
        raise RuntimeError(f"MaaFramework 无法加载 OCR 模型：{OCR_MODEL_ROOT}")

    # Direct offline recognition needs only a Resource. Binding through this official
    # low-level API avoids creating or connecting any device Controller.
    tasker = Tasker()
    tasker._resource_holder = resource
    bound = bool(
        Library.framework().MaaTaskerBindResource(tasker._handle, resource._handle)
    )
    if not bound or not tasker.inited:
        raise RuntimeError("MaaFramework 离线 Tasker 无法绑定 OCR Resource")

    task = tasker.post_recognition(
        JRecognitionType.OCR,
        JOCR(expected=[]),
        image,
    ).get(wait=True)
    if task is None or not task.nodes or task.nodes[0].recognition is None:
        raise RuntimeError("MaaFramework OCR 未返回识别详情")
    return task.nodes[0].recognition.all_results


def main() -> int:
    args = build_parser().parse_args()
    image_path = args.image.expanduser().resolve()
    if not image_path.is_file():
        print(f"错误：图片不存在：{image_path}", file=sys.stderr)
        return 2

    try:
        results = run_ocr(image_path)
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    print("[OCR]")
    print(f"Image: {image_path}")
    print(f"Count: {len(results)}")
    for result in results:
        box = list(result.box)
        print()
        print(f"Text: {result.text}")
        print(f"Confidence: {result.score:.6f}")
        print(f"Box: ({box[0]}, {box[1]}, {box[2]}, {box[3]})")

    recognized = [result.text for result in results]
    print("\n[Targets]")
    for target in TARGET_TEXTS:
        print(f"{target}: {'matched' if target in recognized else 'not matched'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

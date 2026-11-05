# Windows 运行库与分发说明

## Windows 运行库和视频插件

支持 Windows 10/11 x64，使用系统 Universal CRT，不随包分发旧兼容 `api-ms-win-*` 或 `ucrtbase.dll`。其余 VC Runtime 遵循随 Python/Qt 提供的原始许可，不推定任意 Microsoft DLL 均可再分发。

构建依赖固定在 `packaging/requirements.txt`。MMN 使用 OpenCV 图像处理接口，不使用 VideoCapture/VideoWriter，因此排除可选 `opencv_videoio_ffmpeg*.dll`。这不取消其他发行物中已捆绑 FFmpeg 时适用的许可和源码义务；依赖来源记录保留在 `licenses/distribution-evidence-20261104/`。

## 对应源码和库替换

Qt/PySide/Shiboken、MaaFramework 与子模块、MaaDeps/vcpkg、ZeroMQ 和 Eigen 的对应源码、补丁及构建材料见[归档清单](../licenses/corresponding-source-manifest.json)。这些材料与 Windows 二进制同次提供，原始许可证和第三方署名须保留。

LGPL 库通过外部 DLL/PYD 使用，允许为调试修改库而进行必要逆向工程；可利用应用源码、spec、固定依赖及兼容 ABI 的库重新组合。完整性清单用于校验原包，不作为禁止运行修改库的签名锁。修改库替换尚未实测，步骤见[源码与库替换](SOURCE_ACCESS_AND_RELINKING.md)。

## 模型与游戏素材

OCR 使用 PaddlePaddle 官方 Apache-2.0 固定版本 ONNX 原文件，仅重命名而不转换或优化权重。精确来源和哈希见[模型记录](../licenses/ocr/official-models-20261104.json)，官方归档核对记录见 `licenses/distribution-evidence-20261104/official-tar-check.json`。

游戏模板与 GUI 图标的权利范围见[素材说明](ASSET_NOTICE.md)；第三方源码和许可材料不代替游戏素材授权。

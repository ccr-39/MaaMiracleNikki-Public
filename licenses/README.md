# 第三方许可与对应源码

本目录保留运行与构建依赖的原始许可及来源附件。当前方案采用官方固定 ONNX，不分发可选 FFmpeg 或旧兼容 UCRT；具体见[分发说明](../docs/DISTRIBUTION_REMEDIATION_20261104.md)。项目 MIT 不覆盖游戏资源或第三方组件。

## 组件与材料

| 组件 | 版本／来源 | 本目录中的材料 |
| --- | --- | --- |
| Python | 3.13.3 | `Python-3.13.3-LICENSE.txt`，含该发行版的第三方条款 |
| MaaFramework | 5.14.0 | `maafw/` 中的 LGPL-3.0；GPL-3.0 全文另在 `qt/pyside/LICENSES/` |
| MaaAgentBinary、StrEnum | 1.0.1、0.4.15 | 前者随包声明 AGPL-3.0，后者为 MIT；原始文本分别在 `maaagentbinary/` 与 `strenum/`，并固定源码安装所需的这两项传递依赖 |
| Qt、PySide6、Shiboken6 | 6.12.0 | `qt/`，来自官方同版本源码包的许可及第三方声明；采用适用的 LGPL 许可路径，不依据 Commercial 文本主张商业授权 |
| OpenCV Python | 4.14.0.94 | `opencv/` 中原包的 LICENSE 与 LICENSE-3RD-PARTY |
| NumPy | 2.3.1 | `numpy/`，含 OpenBLAS 等随 wheel 组件的声明 |
| MaaDeps | v2.12.6 | `maadeps/` 中 86 个组件的 copyright，另补 ONNX Runtime 1.19.2 第三方声明及 ViGEmClient 原始 MIT |
| DirectML | 1.15.4（MaaDeps 构建清单） | `maadeps/directml-bin/copyright`；专有许可，限适用 Windows/Xbox 应用使用与分发，不改标 MIT |
| Mesa 软件 OpenGL | DLL 中的版本标记 11.2.2 | `mesa-11.2.2/`，含 Mesa 与 LLVM 许可；按 Qt 官方文档作为软件渲染后备库 |
| Microsoft Visual C++ Runtime | 随官方 Python／Qt 包提供 | `microsoft/` 中运行时条款、VS 2022 分发条款及 REDIST 清单；不将其视为开源组件 |
| PyInstaller 启动器 | 6.22.0 | `pyinstaller/` 中 GPL 及其启动器分发例外 |
| OCR 模型与字典 | PaddlePaddle 官方固定版本 ONNX 与已核实字典 | `ocr/`，保留 MaaCommonAssets MIT、PaddleOCR Apache-2.0、上游来源说明和当前模型、字典的来源／哈希 |

Qt/PySide6 源码包中包含多个模块及构建路径，随附声明采用覆盖所选模块的集合，不表示本程序使用了全部模块。没有把商业授权文本作为已购商业许可的证明。

MaaAgentBinary 是 MaaFramework 的独立 Android 代理工具依赖，并非 MaaAssistantArknights。现有固定 EXE 未包含本机 `MaaAgentBinary/` 包目录及其代理二进制，本次不补入、不改用新的输入或截图方式；源码安装时由 pip 获取该独立依赖，其 AGPL 不能改标 MIT。如未来另行捆绑这些代理，应逐个处理其源码与内含组件许可。

## 对应源码与补丁

[corresponding-source-manifest.json](corresponding-source-manifest.json) 固定 13 份归档的版本、URL、SHA256 和大小，并保留已核对的 port SHA512。MaaDeps/vcpkg 归档含 ports、patches 和构建脚本；官方模型信息在 [ocr/official-models-20261104.json](ocr/official-models-20261104.json)。附加 Python 运行时原始声明保存在 `build-runtime-python-20261104/`。

`tools/prepare_distribution.py` 将核验后的 13 份材料放在 Windows ZIP 旁的对应源码目录；未来分发时须同次提供。当前本地备齐不代表已向公众交付，也不代表游戏模板已获分发授权。

## 库替换

Qt/PySide/Shiboken 和 Maa 采用外部 DLL/PYD；不禁止为调试修改库而进行必要逆向工程，可利用兼容 ABI 的库及应用源码、spec、依赖与构建脚本重新组合。详见[源码与库替换说明](../docs/SOURCE_ACCESS_AND_RELINKING.md)。原始完整性清单不作为禁止运行本地修改库的签名锁。

[素材说明](../docs/ASSET_NOTICE.md)中的 22 张 PNG 授权仍待确认，完整内容暂不能公开上传。

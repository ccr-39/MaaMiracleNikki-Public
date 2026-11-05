# 第三方组件、来源与分发边界

本文列出运行与构建依赖的来源、许可及对应源码。项目 MIT 仅覆盖有权授权的原创部分；游戏素材的权属与授权状态见[素材说明](docs/ASSET_NOTICE.md)，非官方身份及使用风险见[免责声明](DISCLAIMER.md)。

## 参考项目与代码来源

- [MaaPracticeBoilerplate](https://github.com/MaaXYZ/MaaPracticeBoilerplate)：package.json 开发依赖和 maatools.config.mts 配置有模板来源，原始 MIT 声明（Copyright (c) 2024 MaaXYZ）保存在 licenses/boilerplate/LICENSE。具体最初模板提交未由历史记录固定，不能把这些部分声明为 MMN 独有原创。
- [MaaFramework v5.14.0](https://github.com/MaaXYZ/MaaFramework/tree/v5.14.0)：通过 Python API 调用资源、控制器、OCR 和任务。按 [LGPL-3.0](https://github.com/MaaXYZ/MaaFramework/blob/v5.14.0/LICENSE.md) 处理；随包原生依赖分别适用自身条款。
- [MaaAssistantArknights](https://github.com/MaaAssistantArknights/MaaAssistantArknights)：参考文档组织与设计思路；其项目声明为 [AGPL-3.0-only](https://spdx.org/licenses/AGPL-3.0-only.html)，不自动覆盖 MMN。gui/emulator.py 标注参考 EmulatorHelper.cs 的 MuMu shutdown 行为；现有来源记录未能确认存在直接复制的源码片段。本地按 MuMu 配置文件端口映射选择实例，没有沿用上游端口换算和进程终止实现。历史来源证据不完整；若发现复制/改写的受保护实现，须补齐原版权和相应许可证并重评整体许可，不能以“参考”规避义务。

## 运行及构建依赖

| 组件 | 本地固定版本 | 许可/附件 |
| --- | --- | --- |
| Python | 3.13.3 | PSF 与随附第三方条款；licenses/Python-3.13.3-LICENSE.txt |
| MaaFramework | 5.14.0 | LGPL-3.0；licenses/maafw 与其原生组件清单 |
| PySide6-Essentials / Shiboken6 / Qt | 6.12.0 | LGPL/GPL/商业许可选项按实际模块处理；本项目未取得商业许可，使用开放许可路径；licenses/qt |
| OpenCV Python headless | 4.14.0.94 | Python 包的 MIT、OpenCV 的 Apache-2.0 及 LICENSE-3RD-PARTY；licenses/opencv |
| NumPy | 2.3.1 | BSD-3-Clause 及 wheel 内 OpenBLAS 等附加声明；licenses/numpy |
| StrEnum | 0.4.15 | MIT；licenses/strenum |
| MaaAgentBinary | 1.0.1（pip 依赖） | 独立 AGPL-3.0 组件；licenses/maaagentbinary。是否捆绑其代理二进制见 [licenses/README.md](licenses/README.md)，不重标 MIT。 |
| PyInstaller | 6.22.0 | GPL 与引导程序分发例外；licenses/pyinstaller |
| MaaDeps / ONNX Runtime / DirectML / Mesa / VC Runtime | 以实际 DLL、源清单为准 | 混合许可证与专有分发条款；licenses/maadeps、mesa-11.2.2、microsoft |

固定构建环境见 packaging/requirements.txt。Node 开发依赖不随 Windows 包捆绑，若后续分发 node_modules，必须另收集传递依赖声明。用户自行取得的游戏、模拟器、ADB 不属于 MMN 分发包。

许可证全文和现有对应源码入口保存在 [licenses/README.md](licenses/README.md)。动态库保持外部 DLL，可用相同 ABI 的修改版本替换；不禁止为调试库修改而进行逆向工程。对应源码的版本、构建材料、修改记录及可取得性须按最终分发方式履行；只有许可证文件不等于完整履行 LGPL。修改库替换尚未实测。

## OCR 模型和字典

当前构建使用 PaddlePaddle 官方固定版本 ONNX 原文件（Apache-2.0），只重命名为 det.onnx / rec.onnx，不转换或优化权重。精确仓库、提交和 SHA256 见 [模型记录](licenses/ocr/official-models-20261104.json)。字典 18,708 项与官方配置一致，来源见 licenses/ocr/keys-provenance-20261104.json。源码只提交安装器和说明，不提交模型二进制；Windows 包包含完整模型和字典。

## 当前发行依赖材料

构建采用固定 headless OpenCV，不分发可选 FFmpeg 和旧兼容 UCRT；Windows 10/11 使用系统 UCRT，其余 Microsoft 运行库仍按原始条款处理。具体说明见[运行库分发说明](docs/DISTRIBUTION_REMEDIATION_20261104.md)。

实际运行时代码还包含 setuptools 80.9.0、packaging 25.0 及其 vendored 组件，原始通知位于 licenses/build-runtime-python-20261104。保留的许可集合可包含可选组件，不能只凭存在某许可证就推定该组件已入包。

13 份对应源码归档覆盖 Qt/PySide、MaaFramework 及子模块、MaaDeps/vcpkg、ZeroMQ 4.3.5 与 Eigen 3.4.1，版本、哈希和补丁位置见[完整清单](licenses/corresponding-source-manifest.json)。本版 Release 已同次提供这 13 份归档、对应源码说明/清单及独立许可附件，后续分发仍须履行适用义务。

## 游戏素材与图标

游戏模板和独立绘制的 GUI 图标分别适用[素材说明](docs/ASSET_NOTICE.md)中的权利范围。游戏资源不因项目开源而获得 MIT 授权；其未确认再分发授权状态不受第三方许可附件影响。系统字体不随包分发。

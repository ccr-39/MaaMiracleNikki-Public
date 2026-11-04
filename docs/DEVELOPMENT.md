# 源码运行与构建

普通 Windows ZIP 用户无需安装 Python，参见[新手教程](USER_GUIDE.md)。本文面向开发者，不提供对游戏或素材的额外授权。

## 环境与资源

推荐 Windows 10/11 x64、Python 3.13.3。应用版本读取 `assets/interface.json`，框架版本见 `agent/requirements.txt`。Node 开发工具为可选依赖，不用于日常运行。

在项目根目录创建独立环境；下面直接使用环境内的 Python，无需激活脚本：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r agent/requirements.txt -r gui/requirements.txt
powershell -NoProfile -ExecutionPolicy Bypass -File tools/setup_ocr.ps1
.\.venv\Scripts\python.exe -m gui.main --dry-run
```

`--dry-run` 仅演示队列，不连接 ADB、不操作游戏；它不代表真实任务已经通过验收。真实任务需另备游戏、兼容模拟器及 ADB，并在界面配置路径与地址。

OCR 安装器校验 `det.onnx`、`rec.onnx`、`keys.txt`，安装与恢复细节见[模型说明](../assets/resource/model/ocr/README.md)。安装器固定获取 PaddlePaddle 官方 ONNX 原文件和既有已核实字典，只重命名模型，不执行权重转换或优化。全新源码副本直接运行安装器即可；已有旧模型默认不会被覆盖，只有明确需要迁移时使用 `-Force`，安装事务仍保留失败回滚。不要根据同名模型或文件哈希推定授权；来源状态见[第三方说明](../THIRD_PARTY_NOTICES.md)。

22 张正式识别模板保留在开发源码中，其再分发授权尚未确认，见[素材清单](ASSET_NOTICE.md)。不要用完整游戏截图、无关图片或空文件替代缺失模板，也不要直接公开包含这些素材的工作目录。

## 静态检查与开发测试

源码、配置模板、测试和必要工具均属于开发文件。以下命令按需要执行，普通用户无需运行：

```powershell
.\.venv\Scripts\python.exe tools/check_project.py --without-private-samples
.\.venv\Scripts\python.exe -m pip install -r packaging/ci-requirements.txt
.\.venv\Scripts\python.exe -m pytest tools -q --without-private-samples
```

部分回归需要未提交的本地截图；缺失样本应明确跳过，不算作通过。详细 GUI 设置与实现见 [GUI 说明](../gui/README.md)。保留的测试用于后续维护，不要求用户运行；旧开发资料在备份分支与 Tag 中保留。

## Windows 构建

先退出 MMN，避免原目录或文件被占用：

```powershell
.\.venv\Scripts\python.exe -m pip install -r packaging/requirements.txt
powershell -NoProfile -ExecutionPolicy Bypass -File tools/build_windows.ps1 -Python .\.venv\Scripts\python.exe
```

产物位于 `dist/MaaMiracleNikki/`，包含主 EXE、独立更新器及 `_internal`。构建工具先在临时目录生成，核对源码和资源并进行离线检查，再安全替换目标目录，保留回滚副本；不要把它当作可直接覆盖正在使用的程序的命令。

本地构建会保留用户数据；发布包必须另从干净目录制作，不能压缩使用后的 `dist`。源码仓库不提交 EXE、DLL、模型、ZIP、userdata、截图、日志、环境、缓存或个人配置。运行包仍必须包含完整 Python/Qt/MaaFramework 运行库和模型，不能照源码的排除规则精简运行包。

参见[目录与回滚](WINDOWS_PACKAGE_LAYOUT.md)、[Windows 打包说明](WINDOWS_PACKAGING.md)与[更新协议](SOFTWARE_UPDATES.md)。

## 未来公开发布

公开仓库计划为 `ccr-39/MaaMiracleNikki-Public`，不会通过公开旧开发仓库来替代该计划。目标同名仓库现为 Private 暂存，两个 Private 仓库均提供 v0.1.2-rc.1 Pre-release，尚未转为 Public，也没有公开 Release；素材授权、发行材料和发布动作见[发布状态](PUBLIC_RELEASE_STATUS.md)。

正式发布前需核对最终程序版本、Releases 附件名、更新器接受的资产格式、ZIP 根目录及清单。手动下载文件名与更新协议示例不能相互替代，须以该次正式发行实际采用的格式为准。旧版发行记录仅供追溯，不应直接作为当前发布操作步骤。

## 导出完整运行包

发行准备使用干净源码副本和固定依赖环境，避免修改日常使用目录。构建结束后，以刚构建的完整运行目录导出：

```powershell
.\.venv\Scripts\python.exe tools/prepare_distribution.py --runtime dist/MaaMiracleNikki --source-archives path/to/corresponding-source --output release-output
```

输出包括 Windows ZIP、SHA256、13 份核验后的第三方源码归档及说明。此工具核对 EXE 内源码与当前源码、运行资源、模型、文件清单及归档哈希，不自动发布。`--runtime` 也可以使用已通过离线启动检查、且能逐模块证明与当前源码相同的运行目录；只改文档无需重复编译相同程序。

未来公开仓库应复制当前提交的文件树，不复制旧 Private 仓库的 Git 历史、备份分支或本地未跟踪文件。两仓库使用同一份正式源码；手动 Actions 构建支持这两个明确仓库名，分支和 Tag 推送不会自动发布。

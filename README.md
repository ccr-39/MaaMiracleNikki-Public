# MaaMiracleNikki · 奇迹暖暖小助手

**MaaMiracleNikki（MMN）** 是基于 MaaFramework 的 Windows 桌面工具，通过图像识别、OCR 和 ADB 辅助完成《奇迹暖暖》国服日常。

提供中文图形界面、任务与子选项选择、进度日志、安全停止、每日定时，以及指定 MuMu 实例的自动启动和完成后关闭。

> **首次公开测试版：[v0.1.2-rc.1](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/tag/v0.1.2-rc.1)（Pre-release）。** Windows 完整 ZIP 解压即可运行，无需安装 Python。项目为非官方工具；游戏素材不属于项目 MIT 授权范围，其再分发授权尚未确认，详见[素材说明](docs/ASSET_NOTICE.md)、[免责声明](DISCLAIMER.md)与[发布状态](docs/PUBLIC_RELEASE_STATUS.md)。

[新手教程](docs/USER_GUIDE.md) · [GUI 说明](gui/README.md) · [文档目录](docs/README.md) · [更新记录](CHANGELOG.md)

## 下载与安装

支持 **Windows 10/11 x64**。普通用户使用 Windows ZIP，**无需安装 Python、Node 或开发工具**；游戏、模拟器和 ADB 需自行准备。

当前版本 **v0.1.2-rc.1 Pre-release**：

**[直接下载 Windows x64 ZIP](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/download/v0.1.2-rc.1/MaaMiracleNikki-Windows-x64.zip)** · [SHA256 校验文件](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/download/v0.1.2-rc.1/MaaMiracleNikki-Windows-x64.zip.sha256) · [Release 页面](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/tag/v0.1.2-rc.1)

普通用户只需下载 Windows ZIP，建议同时下载校验文件。其余 **16 个第三方对应源码及许可附件无需逐个下载**；程序所需运行库和许可说明已随 Windows ZIP 提供。无需 GitHub 仓库授权。该附件是维护者反馈实机测试 10/10 成功的原始 Windows ZIP，本次公开未重打包。测试反馈不代表所有系统和模拟器均已验收。

1. 下载 Release 附件中的 `MaaMiracleNikki-Windows-x64.zip` 和对应 `.sha256`，具体文件名以该版本说明为准。`Source code` 是开发源码，不是可直接运行的 Windows 程序。
2. 完整解压到有写入权限的目录，打开 `MaaMiracleNikki` 文件夹，双击 `MaaMiracleNikki.exe`。不要在 ZIP 内运行，也不要只提取 EXE。
3. 保留同目录的 `_internal`、`MMNUpdater.exe`、`mmn-update.json` 及其余随包文件。首次运行会自动生成 `userdata`，保存配置与日志。

校验前，先打开保存 ZIP 的下载文件夹，在资源管理器地址栏输入 `powershell` 并按回车；在打开的 PowerShell 中执行以下命令，将输出的 `Hash` 与 `.sha256` 文件（可用记事本打开）或下方校验值比较，两者应完全一致（忽略大小写）。命令中的 `./`（下例写作 `.\`）表示当前文件夹；如已将 ZIP 改名，请替换为实际文件名：

```powershell
Get-FileHash .\MaaMiracleNikki-Windows-x64.zip -Algorithm SHA256
```

本版 Windows ZIP 的 SHA256：`d8d23a0a542a150cba89de125ddb77064d48192f0ab8d17aecaf33b1cc07f41b`。为保持已验证 ZIP 完全不变，包内旧文档可能仍有 Private 或候选阶段说明；当前公开下载与发行状态以本仓库 README 和 Release 页面为准。

## 功能

界面共有 **14 个正式任务组**，按固定顺序执行。点击任务旁的齿轮设置子选项。

| 任务 | 内容与重要选项 |
| --- | --- |
| 启动 | 必要时打开游戏、确认更新并进入主页；QQ/微信登录由用户手动完成。 |
| 签到 | 月签到；可选补签及免费/50 钻/100 钻档位、限时活动签到。限时签到只领取免费奖励。 |
| 搭配师联盟 | 委托、图书馆；可选整理资料 3 次，第 2、3 次会消耗金币。 |
| 搭配竞技场 | 免费挑战与奖励领取。 |
| 搭配评选赛 | 默认投票 20 次，可设 1–30 次；可选“我要参赛”。 |
| 织梦人学会 | 委托、免费探索；缺少委托服装时可能用金币补购。 |
| 小屋 | 生活点滴及 5 次拜访。 |
| 好友 | 一键送心。 |
| 体力任务 | 按件数刷取服装，默认 4 件，可设 1–999 件；消耗体力，不购买体力。 |
| 谜之屋 | 免费抽取；可选金币抽取，每日上限 1 或 2 次，分别最多 3000 或 6000 金币。 |
| 世界巡游 | 免费景点游览及相应事件处理。 |
| 服装店 | 用金币购买 3 件“搭配购物节”。 |
| 分解重构 | 分解“无用途”服装并领取奖励，执行前请确认需要保留的服装。 |
| 奖励领取 | 独立选择“邮箱”和“每日任务”；邮箱内可另行开启好友邮件领取。 |

**消费提醒：** 补签可能消耗钻石；联盟额外整理、织梦人补购、谜之屋金币抽取和服装店会消耗金币；体力任务消耗体力；分解重构会改变服装库存。首次使用请少量勾选并观察，不要直接全选。

## MuMu / ADB 与首次使用

ADB 是 MMN 与模拟器通信的工具，首次使用不需要输入命令。先认识三个配置：**下列端口和磁盘路径仅为示例，请使用自己电脑的实际值。**

| 配置 | 怎么找、怎么填 |
| --- | --- |
| 连接地址 | 目标模拟器实例的 ADB 地址，不是随便填写的 IP。在 MuMu 主窗口（多开器）右上角菜单 → 设置中心 → 底部“ADB 端口”查看已运行实例的端口。例如端口为 `16416` 才填 `127.0.0.1:16416`；实例编号不等于端口。 |
| ADB 路径 | 模拟器自带的 `adb.exe` 文件。在安装目录内查找，例如 `D:\MuMuPlayer\MuMuPlayer\nx_device\15.0\shell\adb.exe`；点击此项旁的“选择…”选中文件。找不到示例路径时，从 MuMu 快捷方式“属性 → 目标”找到实际安装目录，再在其中搜索 `adb.exe`。 |
| 自动启动模拟器 | 默认关闭，自己打开模拟器的用户可不填。在“设置 → 定时与启动”勾选“设备未就绪时自动启动模拟器”，通过“启动程序”旁的“选择…”选中实际启动文件，例如 `D:\MuMuPlayer\MuMuPlayer\nx_main\MuMuNxMain.exe`。MuMu 初次建议启动参数留空，具体匹配条件见下方详细教程。 |

首次按以下顺序操作（MuMu 不同版本的菜单位置可能不同，详见 [GUI 新手教程](gui/README.md#mumu-15-从零配置)）：

1. **启动模拟器**：在 MuMu 多开器打开需要使用的实例，开启 ADB 调试，安装《奇迹暖暖》国服中文客户端。建议竖屏 **900 × 1600**。
2. **查找端口**：在多开器按实例名称、编号核对目标窗口，记下这一实例的 ADB 端口；多开时不要选其他账号的实例。
3. **填写连接地址**：打开 MMN“设置 → 连接设置”，填入 `127.0.0.1:自己的端口`，不要直接照抄示例。
4. **选择 ADB 路径**：点击“ADB 路径”旁的“选择…”，选择本机 MuMu 安装目录中的 `adb.exe`。
5. **检测设备**：点击“检测设备”。它只列出当前已连接设备，不扫描端口；列表为空时，保留手填地址，勾选“自动连接指定地址”，先点“连接与截图测试”，成功后再检测。
6. **选择设备**：在“已发现设备”中选中地址相符的在线设备；多开时必须核对端口，不能只选列表第一项。
7. **连接与截图测试**：对本机端口地址勾选“自动连接指定地址”，点击“连接与截图测试”，核对预览是否为目标实例。此按钮不执行游戏任务，也不会启动模拟器。
8. **勾选任务**：手动完成 QQ/微信登录、授权和验证码，回到“每日任务”，第一次先只勾选“启动”，再按需设置其他任务及子选项。
9. **开始**：点击“开始任务”。需要中止时点击“停止”，等待当前操作安全结束；运行时避免手动点击游戏或切换账号、实例。

**“自动连接指定地址”负责建立 ADB 连接，不会打开模拟器；“设备未就绪时自动启动模拟器”负责在开始任务时打开指定实例，两者是独立开关，均默认关闭。** 首次建议先手动启动并完成连接测试，再配置自动启动。

详细的端口查找、安装目录定位、启动参数与故障处理见 [GUI 新手教程](gui/README.md#mumu-15-从零配置)；配置迁移见[新手教程](docs/USER_GUIDE.md)。

### 定时与模拟器启停

在“设置 → 定时与启动”设置每日时间并开启开关；保持电脑开机、MMN 运行，可最小化。程序按电脑本地时间执行已勾选任务，并补上启动游戏；忙碌或错过时间时跳过，不补跑，也不会唤醒电脑。

“设备未就绪时自动启动模拟器”默认关闭，不需要此功能可留空启动程序和参数。MuMu 可选 `MuMuNxMain.exe` 或 `mumu-cli.exe`：参数留空时，须填写本机 ADB 端口地址、保留同目录 `mumu-cli.exe`，且安装目录实例配置中的端口恰好匹配一个实例；匹配失败会报错，不会猜选默认实例。请先核对实际地址、安装目录；仍失败可手动启动，或按 [GUI 说明](gui/README.md#可选自动启动模拟器)填写对应实例的启动参数。该设置也用于手动开始任务。

“设置 → 运行设置 → 全部任务完成后关闭模拟器”需单独开启并指定 MuMu 启动程序。仅非空真实队列全部成功且未取消时，请求正常关闭对应实例；失败、停止、演练或无法唯一匹配时不会关闭。

## 默认设置

| 项目 | 首次启动默认值 |
| --- | --- |
| 全部 14 个任务组、奖励领取子项 | 不勾选 |
| 补签、限时签到、好友邮件、额外整理、我要参赛、金币抽取 | 关闭 |
| 自动连接、失败后继续、离线演练、定时、模拟器自动启动/完成后关闭 | 关闭 |
| 保存上次勾选、启动后自动检查更新 | 开启 |
| 地址、ADB 路径、模拟器路径和参数 | 空，需按自己的设备配置 |
| 投票/体力件数/金币抽取上限/补签档位/定时时刻 | 20 次 / 4 件 / 2 次 / 50 钻 / 08:00；预设数值不会启用对应功能 |

设置自动保存到程序旁的 `userdata/config/gui.json`。默认模板见 [gui.example.json](config/gui.example.json)，无需手动复制。

## 常见问题

| 问题 | 处理方法 |
| --- | --- |
| 双击无窗口、缺 DLL 或资源 | 完整解压并检查安全软件隔离记录，保留 `_internal`；不要单独下载陌生 DLL。 |
| 10061 / 目标计算机积极拒绝连接 | 通常是该端口没有接受连接：先启动目标实例、等 Android 进入桌面，检查 ADB 调试和实际端口，再测试；不要改填任意 IP 或示例端口。 |
| “检测设备”列表为空 | 检测不主动连接。先填写真实地址并选择 ADB，勾选“自动连接指定地址”，点击“连接与截图测试”，成功后重新检测并选择在线设备。 |
| 设备显示 offline（离线）或 unauthorized（未授权） | 等待模拟器完全启动；若弹出 ADB 调试授权则允许。仍离线时正常重启该实例，重新核对端口并测试；详细排查见 [GUI 教程](gui/README.md#连接常见问题)。 |
| 登录一直等待 | 手动处理 QQ/微信授权和验证码；程序不代填账号密码，等待超时后会停止。 |
| 任务没勾选、定时没运行 | 首次全部不选；检查已保存的勾选、定时开关、本地时间和程序是否保持运行。 |
| 任务结束没关闭 MuMu | 检查开关、启动程序和实例映射；失败、部分完成、停止和离线演练均不触发关闭。 |
| 无法检查更新 | 当前发布的是 Pre-release；内置更新只检查稳定版，不提供本测试版，请从上方 Release 链接手动下载。 |

遇到问题请通过 [GitHub Issues](https://github.com/ccr-39/MaaMiracleNikki-Public/issues) 反馈，提供版本、模拟器、分辨率和复现步骤。日志与截图可能含个人信息，请先脱敏；不要上传整个 `userdata`、完整账号画面或 ADB 密钥。

## 源码运行与构建

仅开发者需要 Python，推荐 Windows x64、Python 3.13.3。Node 仅供可选开发工具使用。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r agent/requirements.txt -r gui/requirements.txt
powershell -NoProfile -ExecutionPolicy Bypass -File tools/setup_ocr.ps1
.\.venv\Scripts\python.exe -m gui.main --dry-run
```

构建前退出 MMN，再执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r packaging/requirements.txt
powershell -NoProfile -ExecutionPolicy Bypass -File tools/build_windows.ps1 -Python .\.venv\Scripts\python.exe
```

完整步骤、模型说明、静态检查和发布目录要求见[开发指南](docs/DEVELOPMENT.md)。离线演练不操作游戏，也不代表真实任务已经验收。

## 声明、许可与致谢

MMN 是非官方工具，与游戏开发商、发行商、腾讯、叠纸及 MAA Team 无隶属或背书关系。自动化可能违反游戏服务条款，带来账号限制、误操作和资源损失；使用前请阅读[免责声明](DISCLAIMER.md)。

有权授权的原创代码与文档采用 [MIT License](LICENSE)。**游戏截图、识别模板、美术与商标不属于项目 MIT 授权范围**；第三方代码、模型和运行库遵循各自条款，见[第三方来源](THIRD_PARTY_NOTICES.md)和[素材说明](docs/ASSET_NOTICE.md)。注明来源、非商业用途和免责声明不代替素材授权。

感谢 [MaaFramework](https://github.com/MaaXYZ/MaaFramework)、[MaaPracticeBoilerplate](https://github.com/MaaXYZ/MaaPracticeBoilerplate) 及相关依赖的开发者。README 的组织参考了 [MaaAssistantArknights](https://github.com/MaaAssistantArknights/MaaAssistantArknights)，具体来源关系见第三方说明；MMN 不代表这些项目。

## 分发材料

构建使用已固定的官方 OCR 模型，保留第三方原始许可证；Windows 包排除未使用的 FFmpeg 和旧兼容 UCRT。对应源码的 13 份归档、版本与哈希见[分发材料清单](licenses/corresponding-source-manifest.json)。构建与导出见[开发指南](docs/DEVELOPMENT.md)，这些材料不改变游戏模板授权仍待确认的状态。

# MaaMiracleNikki v0.1.2-rc.2

《奇迹暖暖》国服日常小助手，提供中文图形界面、任务选择、进度日志和安全停止。支持 Windows 10/11 x64，完整解压即可运行，无需安装 Python。

## 功能

支持启动、签到、搭配师联盟、搭配竞技场、搭配评选赛、织梦人学会、小屋、好友、体力任务、谜之屋、世界巡游、服装店、分解重构及奖励领取，共 14 个任务组；支持每日定时和指定 MuMu 实例的自动启动、任务成功后关闭。

## 本版更新

- 清理 Windows 包中的发布前标记与过期说明，随包教程与公开文档保持一致。
- 精简 GUI 使用说明，将线程、内部回调和任务入口实现移至开发文档。
- 重新构建完整 Windows 包；保留原有 14 项任务、GUI 业务逻辑、ADB 连接、随机延迟及用户配置兼容性。

## 下载与安装

**[下载 Windows x64 ZIP](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/download/v0.1.2-rc.2/MaaMiracleNikki-Windows-x64.zip)** · [SHA256 校验文件](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/download/v0.1.2-rc.2/MaaMiracleNikki-Windows-x64.zip.sha256)

1. 下载 ZIP，按下方说明校验后，完整解压到有写入权限的目录。不要在 ZIP 内运行，也不要只提取 EXE。
2. 打开 `MaaMiracleNikki` 文件夹，双击 `MaaMiracleNikki.exe`；保留 `_internal`、更新器和其他随包文件。
3. 自行准备游戏、模拟器及 ADB，按[新手教程](USER_GUIDE.md)填写目标实例的 ADB 地址和路径，完成连接与截图测试。
4. 手动完成游戏登录，首次先只选“启动”，再逐项开启需要的任务和子选项。

普通用户只需 Windows ZIP，建议同时下载校验文件；其余 16 个第三方源码及许可附件无需逐个下载，运行所需文件和许可说明已在 ZIP 中。

### SHA256 校验

打开保存 ZIP 的文件夹，在资源管理器地址栏输入 `powershell` 并回车，执行：

```powershell
Get-FileHash .\MaaMiracleNikki-Windows-x64.zip -Algorithm SHA256
```

用记事本打开同一 Release 的 `.sha256` 文件，将其中的 SHA256 与命令输出的 `Hash` 比较，应完全一致（忽略大小写）。

## 使用提醒与反馈

- 首次安装全部任务默认不勾选。补签可能消耗钻石；联盟额外整理、织梦人补购、谜之屋金币抽取及服装店会消耗金币；体力任务消耗体力，分解重构会改变库存。运行前核对选项。
- 本版为 Pre-release，内置稳定版更新不会自动提供；请手动下载。游戏更新、模拟器版本或分辨率变化可能影响识别。
- 详细设置见 [GUI 教程](../gui/README.md)和[完整功能说明](../README.md#功能)。遇到问题请通过 [GitHub Issues](https://github.com/ccr-39/MaaMiracleNikki-Public/issues) 反馈版本、模拟器、分辨率与复现步骤，日志和截图先脱敏。

## 声明

MMN 为非官方工具，与游戏运营方及 MAA Team 无隶属或背书关系。自动化可能违反游戏服务条款并带来账号、资源或数据损失。有权授权的原创部分采用 MIT，游戏素材和第三方组件遵循各自权利与许可范围。使用前请阅读[免责声明](../DISCLAIMER.md)、[素材说明](ASSET_NOTICE.md)和[第三方声明](../THIRD_PARTY_NOTICES.md)。

## 第三方许可与对应源码

<details>
<summary>许可、版本清单与对应源码下载（供开发者和再分发者）</summary>

- [许可证与来源说明 ZIP](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/download/v0.1.2-rc.2/MMN-v0.1.2-rc.2-licenses.zip)
- [对应源码说明](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/download/v0.1.2-rc.2/MMN-v0.1.2-rc.2-corresponding-source-README.md)
- [版本、来源与 SHA256 清单](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/download/v0.1.2-rc.2/MMN-v0.1.2-rc.2-corresponding-source-manifest.json)

以下对应源码归档与二进制同时提供；MaaDeps/vcpkg 中包含 ports、补丁和构建材料，库替换说明见[对应源码与库替换](SOURCE_ACCESS_AND_RELINKING.md)。

- [eigen-3.4.1.tar.gz](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/download/v0.1.2-rc.2/eigen-3.4.1.tar.gz)
- [EmulatorExtras-54d3a3ad448f0541df3759ae91b571a6762daaf1.tar.gz](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/download/v0.1.2-rc.2/EmulatorExtras-54d3a3ad448f0541df3759ae91b571a6762daaf1.tar.gz)
- [libzmq-v4.3.5.tar.gz](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/download/v0.1.2-rc.2/libzmq-v4.3.5.tar.gz)
- [MaaAgentBinary-173d43c4b5518064b27720b0cf08c781f291eba9.tar.gz](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/download/v0.1.2-rc.2/MaaAgentBinary-173d43c4b5518064b27720b0cf08c781f291eba9.tar.gz)
- [MaaDeps-v2.12.6.tar.gz](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/download/v0.1.2-rc.2/MaaDeps-v2.12.6.tar.gz)
- [MaaFramework-v5.14.0.tar.gz](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/download/v0.1.2-rc.2/MaaFramework-v5.14.0.tar.gz)
- [MaaUtils-6e9ba33f6ad835418097d9324c01c44a82825a2b.tar.gz](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/download/v0.1.2-rc.2/MaaUtils-6e9ba33f6ad835418097d9324c01c44a82825a2b.tar.gz)
- [pyside-6.12.0.tar.xz](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/download/v0.1.2-rc.2/pyside-6.12.0.tar.xz)
- [qtbase-6.12.0.tar.xz](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/download/v0.1.2-rc.2/qtbase-6.12.0.tar.xz)
- [qtimageformats-6.12.0.tar.xz](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/download/v0.1.2-rc.2/qtimageformats-6.12.0.tar.xz)
- [qtsvg-6.12.0.tar.xz](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/download/v0.1.2-rc.2/qtsvg-6.12.0.tar.xz)
- [qttranslations-v6.12.0.tar.gz](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/download/v0.1.2-rc.2/qttranslations-v6.12.0.tar.gz)
- [vcpkg-84bab45d415d22042bd0b9081aea57f362da3f35.tar.gz](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/download/v0.1.2-rc.2/vcpkg-84bab45d415d22042bd0b9081aea57f362da3f35.tar.gz)

</details>

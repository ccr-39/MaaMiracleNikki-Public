# 公开发布状态

当前版本为 **v0.1.2-rc.1 Pre-release**，两个仓库目前均保持 **Private**：

- [旧仓库版本页](https://github.com/ccr-39/MaaMiracleNikki/releases/tag/v0.1.2-rc.1)：Tag 固定于已实机验证的 b155f7d 源码。
- [新暂存仓库版本页](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/tag/v0.1.2-rc.1)：仓库虽名为 MaaMiracleNikki-Public，当前仍是 Private；源码使用全新根提交，不携带旧 Private 历史。

只有获得仓库访问权限并登录 GitHub 的用户可以查看和下载；无权限时可能显示 404。两处 Windows ZIP 逐字节相同，SHA256 均为 `d8d23a0a542a150cba89de125ddb77064d48192f0ab8d17aecaf33b1cc07f41b`，没有重打包维护者反馈实机 10/10 成功的原始包。业务逻辑仍以 b155f7d 为基准，本次只更新源码中的发布说明和下载链接；包内文档保留验证时的未公开状态，当前入口以仓库 README 和 Release 页面为准。

**公开上传仍暂停。** 当前源码与 Windows 包保留 22 张必要局部 PNG 和 2 张独立重绘 GUI SVG。PNG 的再分发权限尚未确认；只有解决此项，才能公开含这些素材的源码与 ZIP。MIT 只适用于有权授权的原创部分，不覆盖游戏画面和商标。见[素材清单](ASSET_NOTICE.md)。没有因 Private 暂存或测试成功而将其写成已经取得授权。

两个 Private Pre-release 均随 Windows ZIP 提供 SHA256、13 份对应源码归档、对应源码 README/manifest 和独立许可证附件；版本、归档哈希与补丁材料沿用[既有分发方案](DISTRIBUTION_REMEDIATION_20261104.md)。这表示已向获准访问的仓库用户提供附件，不表示已经向公众交付，也不替代将来公开时的许可义务。

未修改业务逻辑，未连接 ADB 或重复游戏测试，未覆盖固定 EXE。维护者反馈原始包实机测试 10/10 成功；不将其扩大为所有任务、系统或模拟器均已验收。

公开前需补齐覆盖 22 个具体文件及源码仓库/Windows ZIP 分发方式的权限依据，核对声明与附件，再将同名暂存仓库转为 Public；旧仓库始终保持 Private。内置更新只检查公开稳定版，当前 Private Pre-release 不会经自动更新提供。

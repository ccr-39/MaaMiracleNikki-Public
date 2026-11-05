# 手动 Windows 构建

`.github/workflows/windows-release.yml` 仅由 `workflow_dispatch` 手动触发；推送 main 或 Tag 不自动构建，也不上传 Release。

支持旧 Private 开发仓库 `ccr-39/MaaMiracleNikki` 和已公开的 `ccr-39/MaaMiracleNikki-Public`，使用同一份源码。版本从 `assets/interface.json` 读取；Windows x64、Python 3.13.3 和构建依赖由工作流及 requirements 固定，官方 Actions 固定到提交 SHA。

流程包含源码与配置检查、固定模型准备、保留测试的离线回归、安全 onedir 构建和本地资产导出。手动产物是带 commit 标记的测试构建，不等于正式公开发行。

对外分发的完整包与对应源码材料应使用[开发指南](DEVELOPMENT.md)中的 `prepare_distribution.py` 准备，核对全部来源附件和文件清单。正式版本的在线更新资产规则见[更新协议](SOFTWARE_UPDATES.md)。

公开仓库为 [MaaMiracleNikki-Public](https://github.com/ccr-39/MaaMiracleNikki-Public)，已提供 [v0.1.2-rc.1 Pre-release](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/tag/v0.1.2-rc.1)。手动构建不会自动替换现有 Release 附件；发布时须核对发行材料，保留已发布的 Tag 和附件。版本与下载入口见[发布与下载](PUBLIC_RELEASE_STATUS.md)。

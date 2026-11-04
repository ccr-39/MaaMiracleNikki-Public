# 手动 Windows 构建

`.github/workflows/windows-release.yml` 仅由 `workflow_dispatch` 手动触发；推送 main 或 Tag 不自动构建，也不上传 Release。

支持旧开发仓库 `ccr-39/MaaMiracleNikki` 和未来 `ccr-39/MaaMiracleNikki-Public`，使用同一份源码。版本从 `assets/interface.json` 读取；Windows x64、Python 3.13.3 和构建依赖由工作流及 requirements 固定，官方 Actions 固定到提交 SHA。

流程包含源码与配置检查、固定模型准备、保留测试的离线回归、安全 onedir 构建和本地资产导出。手动产物是带 commit 标记的测试构建，不等于正式公开发行。

对外分发的完整包与对应源码材料应使用[开发指南](DEVELOPMENT.md)中的 `prepare_distribution.py` 准备，核对全部来源附件和文件清单。不要将旧版固定 EXE 上传工具或临时审计结果当作当前发行流程。正式版本的在线更新资产规则见[更新协议](SOFTWARE_UPDATES.md)。

素材授权和公开动作仍受[发布状态](PUBLIC_RELEASE_STATUS.md)约束；当前未创建公开仓库或 Release。

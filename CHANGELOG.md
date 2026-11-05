# 更新记录

## 0.1.2-rc.2（Pre-release）

- 修复发行包保留发布前状态说明的问题，随包教程与公开文档同步。
- 精简 GUI 用户说明，将线程、回调和任务入口实现移至开发文档。
- 更新真实 GUI 默认配置预览及 Windows 发行材料，保留 14 项任务、连接方式、随机延迟与配置兼容性。

[下载本版](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/tag/v0.1.2-rc.2) · [安装教程](docs/USER_GUIDE.md)

## 0.1.2-rc.1（Pre-release）

### 新增与改进

- 提供 Windows 中文图形界面和 14 个日常任务组，支持任务子选项、进度日志与安全停止。
- 增加限时活动签到；支持补签费用档位，以及邮箱、每日任务和好友邮件的独立选择。
- 支持每日定时、指定 MuMu 实例自动启动，以及全部任务成功后关闭模拟器；自动启停默认关闭。
- 完善 MuMu/ADB 配置教程和常见连接问题说明。

### 修复

- 修复搭配评选赛漏读选手姓名时的刷新超时，补充局部 OCR 识别和返回主页处理。
- 减少签到流程中的无效识别，保留补签费用确认和奖励处理。
- 登录页面等待用户手动完成 QQ/微信授权；无法确认页面时安全停止。

[下载本版](https://github.com/ccr-39/MaaMiracleNikki-Public/releases/tag/v0.1.2-rc.1) · [版本说明](docs/RELEASE_NOTES.md) · [新手教程](docs/USER_GUIDE.md)

## 0.1.1-dev

开发阶段提供 Windows GUI、任务编排、ADB 配置、定时、恢复与更新组件。

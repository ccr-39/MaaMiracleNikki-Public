# 配置保存与旧版迁移

EXE 配置固定保存在其目录 `userdata/config/gui.json`，日志、缓存、状态和运行保护记录位于同一 `userdata`。原目录覆盖更新保留，新目录从默认值开始。源码默认使用源码目录 `userdata`，开发测试可设置 `MMN_DATA_DIR`。

启动时仅检查当前安装目录旧 `config/gui.json`。若新配置不存在，先校验旧配置，再复制完整字节，同时保存 `userdata/config/migration-backups` 备份及 `gui-migration-v1.json` 报告。原文件不删除。新配置始终优先，冲突不覆盖；无旧配置不创建完成标记。不搜索上级源码、其他安装目录或共享 LocalAppData。发行资源不是迁移来源。

手动迁移：关闭程序，备份目标 `userdata`，将来源配置复制到目标 EXE 同目录 `userdata/config/gui.json`。旧共享配置可在 `%LOCALAPPDATA%/MaaMiracleNikki` 或 `%LOCALAPPDATA%/MaaMiracleNikki-Public` 下找到，不会自动读取或清空。只迁移 GUI 设置不会复制日志或完成状态；完整运行数据需手动复制 `userdata` 并核对账号及消费保护记录。

自动保存保留现有原子写入、文件同步及 400ms 合并机制。输入无效或目录不可写时显示错误。请使用有写权限的普通目录，更新前退出程序并备份。不要通过删除保护记录绕过未决消费核查。

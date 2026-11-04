# 固定 Windows EXE 目录与安全替换

正式入口始终为工程根目录下的 `dist/MaaMiracleNikki/MaaMiracleNikki.exe`。
桌面快捷方式只需指向这个文件。不要把 EXE 与 `_internal` 分开更新。

## 唯一日常构建命令

```powershell
powershell -ExecutionPolicy Bypass -File tools/build_windows.ps1
```

保留 `-Python <python.exe>` 和 `-InstallDependencies`。脚本调用
`tools/windows_package.py`，使用同一份 spec，保持 PyInstaller onedir 与无控制台模式。
不直接构建到正式目录，不创建日期或版本号命名的正式目录。

执行流程：

1. 获得构建锁；检查 MMN 进程及固定目录的 Windows 删除/改名访问权。
   只读检查，不强制结束进程。ADB server 或终端的 cwd 也可能占用目录。
2. 如果上次替换中断，先根据 `build/windows-package-transaction.json` 恢复。
3. 在本轮专属 `build/.windows-package-*` 下调用 PyInstaller。
   编译日志写到 `build/windows-package-build.log`，失败不会动旧发行版。
4. 检查 EXE、Python、Qt、Maa DLL、OCR、Pipeline；对比打包字节码与源码、
   资源哈希，拒绝缺失或过期产物、构建期间变化的输入。
5. 复制程序至临时目录，直接执行 EXE 的 `--offline-check`，验证其同目录独立配置；不打开真实用户配置。
6. 再次检查程序关闭状态，写入持久化事务记录。旧正式目录整体改名到
   `build/windows-package-backup/MaaMiracleNikki`，完整新目录整体改名到正式路径。
7. 对正式路径再次执行离线检查。失败时自动恢复旧目录；若占用导致恢复也失败，
   保留旧备份、暂存目录和事务记录，提示关闭占用后再次执行同一命令。
8. 新版确认成功后仅保留一份回滚备份，清理本轮暂存文件。
   较早备份必须与记录的文件清单完全一致，且不包含 userdata/config/cache/logs/state/images，
   才允许淘汰；发现陌生文件则保留并拒绝静默删除。

`_internal/mmn-release.json` 记录本次代码/资源哈希及事务 ID。
检查报告位于 `build/windows-package-report.json`、
`windows-package-stage-check.json`、`windows-package-final-check.json`。
备份中的 `_internal/mmn-backup-files.json` 用于核对旧备份是否被用户改动。

`tools/verify_windows.ps1` 仍可独立执行，默认验证固定目录。可选
`-ReportPath <file.json>` 保存检查结果；通过后只清理自己创建的隔离目录，
失败则保留诊断文件并打印位置。它不会使用真实用户配置或执行真实任务。

打包入口会在启动 GUI 前切换至系统临时目录，避免新启动的长期子进程继承
发行目录并锁住后续更新。Python 开发入口 `python -m gui.main` 保持不变。

## 数据和业务保护

GUI 配置、模拟器路径、定时、任务选项、日志、状态、缓存和迁移记录在程序目录 `userdata`。
替换时复制保留本地数据，旧版完整备份同时保留；失败回滚恢复旧目录。共享 LocalAppData
保持原样，不自动导入。校验使用临时程序副本，不写当前配置。
15 项任务、GUI 布局与交互、随机点击延迟和织梦人入口修复均未修改。

`--reuse <完整发行目录>` 仅供维护整理：必须通过同样的源码/资源与离线检查，
不会绕过验证或生成新的正式路径。日常构建无需此参数。


离线启动验证由构建工具调用 `tools/verify_windows.ps1`；不连接 ADB、不执行游戏任务。发行目录与 13 份源码材料使用 `tools/prepare_distribution.py` 生成，见[开发指南](DEVELOPMENT.md)。

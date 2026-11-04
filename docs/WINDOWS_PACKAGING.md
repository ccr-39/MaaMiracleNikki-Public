# Windows 完整运行包

GUI 从 `gui.main` 启动，在同一进程使用正式任务入口。界面有 14 个任务组，奖励领取下含邮箱和每日任务两个执行子项。主程序为 PyInstaller onedir，独立更新器为 onefile；两个 EXE 均位于运行目录根部。

运行目录必须保留 `_internal`、Python DLL/标准库、Qt 平台插件、MaaFramework 原生库、OpenCV、NumPy、全部 Pipeline、22 张 PNG、2 张 SVG 及 OCR 模型/字典。用户完整解压即可启动，不需要安装 Python。游戏、模拟器及 ADB 由用户提供。

固定环境使用 `packaging/requirements.txt`；仅安装 headless OpenCV，排除可选 `opencv_videoio_ffmpeg*.dll`。支持 Windows 10/11 x64，使用系统 UCRT，不分发旧兼容 `api-ms-win-*` 和 `ucrtbase.dll`。其他可能动态加载的运行库保守保留，不按文件名猜测删除。

构建命令与导出步骤见[开发指南](DEVELOPMENT.md)。构建在独立 staging 目录完成，核对当前源码和资源，离线启动检查后再安全部署，并保留失败回滚；不要直接覆盖运行中的目录。

用户数据位于安装目录的 `userdata`；资源按 `_MEIPASS` 定位，不依赖工作目录。源码构建目录可保留个人数据，但发行包必须从干净目录生成，不包含用户数据、截图、测试、缓存或本机路径。旧配置迁移见[配置说明](CONFIGURATION_MIGRATION.md)。

运行包内的原始许可证与对应源码获取说明必须随包保留。完整对应源码归档在 ZIP 之外作为同次发行附件准备，不能仅放项目 MIT 或未创建的下载链接。当前素材授权状态见[发布说明](PUBLIC_RELEASE_STATUS.md)。

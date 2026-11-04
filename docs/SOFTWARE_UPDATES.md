# Windows 在线更新（第二阶段）

第三阶段工作流、Private 验证与首次公开发布步骤见 [GitHub Actions 发布](GITHUB_ACTIONS_RELEASE.md)。

应用版本的唯一来源是 `assets/interface.json` 的 `version`，`updates/version.py` 提供严格 SemVer 比较，Qt 元数据、设置页、打包清单使用相同值。当前 `0.1.2-rc.1` 不会自动变成正式版本；发行前需显式设置稳定版本。

## 用户流程

普通 GUI 启动两秒后，在后台匿名请求配置的发行仓库 `ccr-39/MaaMiracleNikki-Public`（当前为 Private 暂存，尚未公开） 的 `/releases/latest`。仅接受稳定版本，不使用文件时间。该接口、权限及匿名访问规则见 [GitHub Releases 官方文档](https://docs.github.com/en/rest/releases/releases#get-the-latest-release)。无网络、404、限流和超时只在设置中显示状态，不弹窗、不阻塞任务。离线演练命令及 `--offline-check` 不触发网络。

“设置 → 软件更新”显示当前版本、最新稳定版、纯文本发行说明、进度和状态。可关闭启动检查或手动检查；发现更新时设置标签显示“有更新”。点击“下载更新”并确认后下载；下载完成后仍需点击“安装并重启”确认。任务运行时可选择等待队列完成，或通过现有安全停止流程停止后安装，也可取消安装等待。等待安装期间禁止启动新队列和定时任务。界面为 14 个正式任务组，奖励领取组展开为邮箱和每日任务两个执行入口，共 15 个执行入口。

HTTP 连接/读取超时为 10 秒，可重试的网络故障最多重试两次，退避 0.5/1 秒；单次包下载最长 30 分钟。不续传，失败重试从头下载。取消检查会在当前有界网络读取返回后生效；部分文件自动清理。通过校验的包保留在独立系统临时目录 `mmn-download-*`。

## 发行资产协议

以下为当前更新器接受的版本化命名示例，不代表这些附件已上传。手动下载说明中的无版本文件名也不能据此宣称已适配在线更新；正式发布时需核对该次实际附件与客户端规则。

例如 tag `v1.2.3`（也支持 `1.2.3`），需要两个同一 Release 的资产：

```text
MaaMiracleNikki-1.2.3-windows-x64.zip
MaaMiracleNikki-1.2.3-windows-x64.zip.sha256
```

SHA256 文件必须为 ASCII，内容为 `64位十六进制哈希`、两个空格、ZIP 文件名和可选末尾换行。GitHub 元数据提供资产 digest 时还会交叉核对。

ZIP 中只有 `MaaMiracleNikki/` 一个根目录，包含主 EXE、`MMNUpdater.exe`、完整 `_internal`，以及根目录 `mmn-update.json`。清单 schema=1，含 product、platform=`windows-x64`、version 和每个发行文件的 SHA256（不包含清单自身）。应用 interface 版本、Release 版本、清单版本必须一致。清单版本允许开发版本用于本地构建，但导出工具拒绝开发版本，在线客户端拒绝预发布。

下载入口必须精确匹配官方仓库及 tag 的 `browser_download_url`，只允许 HTTPS，并限制后续跳转到 GitHub 的发布资产域名。不会下载源码 zipball 或执行包内脚本。检查重复路径、Windows 大小写冲突、绝对路径、盘符、ADS、保留设备名、`..`、反斜杠、链接、特殊文件和加密文件；限制最多 50000 个文件、2 GiB 下载、6 GiB 解压和 8 MiB 清单。提取前校验路径与清单集合，提取后校验逐文件哈希及现有 onedir 必备组件。

SHA256 保证下载完整性，信任基础是 HTTPS 和官方仓库权限；本阶段未实现独立签名验证。客户端不需要 GitHub 登录或 Token。

## 独立更新与回滚

GUI 使用当前已安装且哈希与本地发行清单一致的独立更新器。将其复制到系统临时目录，为每次尝试建立独立请求/取消/就绪文件；不执行下载包里的更新器或更新脚本。独立更新器为标准库 onefile EXE，主程序继续使用 onedir，不需要用户安装 Python。

安装目标由运行中 EXE 的位置确定，不依赖开发者路径。GUI 先保存配置，然后异步等待更新器持有本次主进程句柄并报告就绪，才退出事件循环。独立更新器等待该进程退出，检查其他 MMN 实例及目录 DELETE 权限，再执行更新。握手约 20 秒、退出等待最多 300 秒；超时保留原版。不会强制结束任何进程。

每个安装位置有同级 `.mmn-update-MaaMiracleNikki/`，其中独占锁避免并发，`transaction-*` 保存候选、备份及失败目录，`transaction.json` 记录可恢复事务。候选解压到安装目录同一卷，校验后复制旧版便携配置，再将原目录改名为备份，整目录替换，最后再次校验。新版以隐藏探测模式启动，暂停定时及任务，在 GUI 构建和事件循环就绪后写入带随机凭证和 PID 的健康回执；更新器在 30 秒内确认回执后提交，新版才显示窗口并恢复定时。进程创建或启动探测失败则通知新版退出，回滚并尝试启动旧版；不强制结束无响应的进程。备份不自动删除，避免误删旧版未知用户文件，可在确认新版可用、所有 MMN 和更新器退出后人工清理。

任一步骤失败均尝试恢复备份；回滚自身被占用阻止时保留备份和事务，更新器提示用户关闭占用程序后“重试”。下次更新器运行也先恢复未完成事务。系统断电、磁盘故障或永久权限问题不能保证自动恢复，需保留同级事务目录处理。启动回执只覆盖 GUI 初始化与事件循环，不执行游戏任务，也没有持续运行期间的自动降级监控。

默认用户数据为 EXE 同目录 `userdata`，原目录更新时复制保留，旧备份同时保留。旧便携目录的 `config/cache/logs/state/images` 及 `gui.json/runtime.json/device.json`（根目录或 `_internal`）也复制保留；包内禁止提供这些文件。安装目录内仅允许标准 `userdata`，其他内嵌数据目录或符号链接/重解析点拒绝更新。更新器自身、工作目录和请求文件放在安装目录外，避免更新时文件锁定。共享 LocalAppData 不自动导入、不修改。Python 开发模式仅支持检查/下载，安装器拒绝替换源码目录。

## 打包及第三阶段接口

现有 `tools/build_windows.ps1` 继续私有 staging、验证、部署到 `dist/MaaMiracleNikki/MaaMiracleNikki.exe`，保留开发测试固定路径。spec 新增独立更新器，将两个 EXE 对象交给同一个 COLLECT，保持更新器位于发行根目录；参考 [PyInstaller spec 官方文档](https://pyinstaller.org/en/stable/spec-files.html)。构建验证会读取主程序及更新器归档、比对当前源码，生成发行清单，并在写入部署事务 ID 后重新计算清单。没有对运行中目录直接构建的操作。

以下基线构建说明保留自更新功能开发阶段，不代表本轮文档整理执行了打包或发布。已有旧 EXE 无法凭源码修改获得更新能力，需确认后构建一次带更新器的基线发行版。现有不含更新器/清单的构建不能通过 `--reuse` 升级为新基线。

已构建且验证的稳定发行目录可用以下命令导出本地资产，不发布：

```powershell
python tools/release_bundle.py --release dist/MaaMiracleNikki --output build/release-assets
```

第三阶段 GitHub Actions 需：Windows x64 / 固定 Python 和依赖；准备已校验 OCR 资源；校验 tag 与 interface 版本一致；运行完整离线回归；在独立工作区构建并验证两个 EXE；调用上述工具生成 ZIP/SHA256；仅在授权的正式 tag 流程上传到对应稳定 Release 并填写中文说明。还应增加干净 Windows、中文/空格路径下的真实打包升级验收；可进一步引入 Authenticode、独立签名清单与备份清理策略。用户更新不需要 Actions 权限或 Token。

## 离线验证

```powershell
python -m pytest tools/test_software_update.py tools/test_software_update_gui.py tools/test_windows_package.py -q
python -m pytest tools -q --without-private-samples
python -m gui.main --offline-check
```

新测试使用模拟 GitHub JSON、内存 HTTP 响应、本地小 ZIP 和不可执行的 EXE 字节夹具；更新器的重启边界使用 mock。真实文件占用测试只启动测试专用 Python 子进程，以关闭 stdin 的方式让它自行退出。不会下载/执行远程程序、连接 ADB、启动模拟器或执行游戏任务。缺少私人截图的测试明确跳过。

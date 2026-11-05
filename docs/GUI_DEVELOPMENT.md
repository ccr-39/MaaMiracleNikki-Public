# GUI 开发与任务集成

本文面向开发维护；用户操作见 [GUI 使用说明](../gui/README.md)。

在项目根目录运行（推荐 Python 3.13.3 x64）：

```powershell
python -m pip install -r gui/requirements.txt
python -m pip install -r agent/requirements.txt
python -m gui.main
# 或 python gui/main.py
# 强制本次会话离线演练：
python -m gui.main --dry-run
```

正常 GUI 默认使用实机模式；只有点击 Start! 才在 QThread 中连接或确认设备并执行任务，
打开 GUI 不连接 ADB。可在设置页勾选“离线演练（不连接设备）”，自动保存；
`--dry-run` 则强制本次会话离线，不能在界面切回实机，且不覆盖配置中的模式。
旧 GUI 配置缺少 `dry_run` 字段时按实机模式读取。
离线模式每项约 0.6 秒，不创建 ADB Controller、Resource 或 Tasker；该模式下
“已完成”只表示离线队列完成。窗口标题和运行状态明确显示模式。

配置独立保存在用户数据目录下的 `config/gui.json`，旧项目配置仅用于兼容迁移。
连接地址、ADB 路径、自动连接、执行模式、失败处理、日志等级、任务勾选和任务参数统一自动保存。
文字输入停止约 400 毫秒后保存，开始任务和关闭窗口前立即保存；输入无效时保留上一份有效配置，开始任务会阻止执行并提示修正。关闭窗口会放弃无效草稿，保留有效设置和当前任务选择。写入失败时显示“重试保存”。
关闭“保存上次勾选”后，下次启动使用注册表默认值。连接测试只表示测试当时成功，开始任务时仍会重新确认连接。
每日任务页将连接信息分为三项：“目标设备”显示地址与保存状态；“截图测试”显示本次打开期间的最近一次测试结果和时间；“本次连接”只依据正式任务的连接、初始化和环境释放事件更新。检测设备不等于截图测试通过，测试通过不等于任务正在连接。任务完成或停止后显示会话结束，不宣称 ADB 已断开；重新打开助手时测试和运行状态重新确认，不沿用历史成功标记。
当前共有 14 个 GUI 主任务，其中“奖励领取”包含 2 个子项。新配置所有任务均不勾选；奖励领取及两个子项的注册默认值也均为不勾选。
已有勾选配置按入口恢复，不自动勾选新增项。配置原子写入，读写错误在界面显示。
日志等级控制 GUI 调度日志，输出到启动终端；现有业务日志保持原实现。
每日任务页右侧只订阅 `QueueWorker.user_event`，事件契约在 `user_events.py`：
每个事件携带类型、时间、等级、中文文本及可选任务名称。
调度层明确发布连接、开始、完成及用时、失败、停止、队列完成和环境释放事件。
`business_events.py` 兼容已有业务输出，按当前正式任务和支持的完整格式/节点事件
转换次数、投票进度、无免费机会、签到/邮箱跳过、好友送心结果和返回主页等关键结果。
它采用已知来源的窄范围映射，不按关键词黑名单粗暴删日志，也不据日志推断任务成功或失败。
未知文本、原始 focus、GlobalTransientGuard、StateRecovery、内部节点、分数、OCR 坐标、
对象 repr、原始回调、逐帧信息、重试和 traceback 都不会直接进入普通 GUI 日志。

每次运行完整 Python stdout/stderr、已输出的 logging（含异常栈）和原始框架回调保存到
用户数据目录下的 `images/debug/gui/runtime-日期-时间-唯一编号.log`；不截断原始长行，不按 GUI 上限删除，
终端输出也保留。运行结束后，将鼠标停在日志区域可查看该次详细文件路径。
MaaFramework 原生 C++ 详细日志使用用户数据目录下的 `images/debug/pipeline/maafw.log` 及其原有备份，
不重定向系统文件描述符、不修改框架日志策略。GUI 默认不展示详细文件内容。
界面显示上海本地时间和 INFO / SUCCESS / WARNING / ERROR，以轻微颜色区分。
任务列表右侧显示本批状态：等待、执行中、完成、跳过、失败、已停止或未执行。
执行中持续更新用时，结束后显示工作线程记录的实际用时；列表顶部显示完成/跳过数量与总数。
鼠标悬停任务可查看结果或跳过原因。失败、停止和连接失败后，尚未开始的任务标为“未执行”。
只有任务正式成功返回才确认“完成/跳过”，普通进度日志不作为完成依据。
离线模式标明“待演练/演练中/演练完成”，不代表游戏内已完成。
本批结果保留至下一次开始任务；新一批仅重置状态，不改变任务名称、顺序或勾选。
失败或停止后，状态区显示简短提示和恢复按钮：“重试失败项”只执行失败任务，
“继续未完成”执行失败、已停止和未执行的任务，两者都保留完成/跳过的结果及原始勾选。
重试会创建新的运行会话，仍通过原任务入口的页面校验和已有购买/领取检查点，用户应先确认游戏回到主页。
连接失败时可点击“重新连接”跳到连接设置并读取设备信息和截图，不自动执行游戏任务。
已确认的 ADB 连接错误与资源/运行环境初始化错误分别显示建议；无法确认原因的任务失败不推测成识别超时或登录问题。
连接地址或执行模式改变后，应通过“开始任务”开始新一批，避免将不同设备的任务结果混在一起。
日志只读，最多保留最近 2000 条；支持清空。默认自动滚动，手动上滚时暂停跟随，
回到底部后恢复跟随。当前任务说明缩小并保留在状态区。
后台及 Maa 回调线程的用户事件经 `QueueWorker.user_event` 信号传递到 GUI 槽函数，
包括任务开始/完成、耗时、失败原因、用户停止、后续中断及环境释放。
事件级别只影响显示颜色，不影响现有任务成功/失败判断。
修改设置后自动保存。运行期间参数和任务勾选锁定，仍可切换查看任务；下一批使用最新有效设置。

ADB 地址可以是 TCP endpoint 或已在线的 USB serial。自动连接开启时，对于 TCP 地址
复用 `device_manager.connect_endpoint` 执行一次 `adb connect`，再确认指定设备在线、
已授权并安装目标游戏。关闭时只确认指定设备已在线，不执行 `adb connect`。
GUI 初始化不使用 MuMu 默认地址 fallback，不因其他实例前台而更换目标。
设置页可以选择模拟器自带的 ADB，支持 `adb.exe`、`HD-adb.exe`、`nox_adb.exe` 等名称。
也可填写 PATH 中的 `adb`，或相对于项目根目录的 `platform-tools/adb.exe`。
明确指定的路径无效时会报错，不会改用别的 ADB；留空才沿用原自动查找规则。
连接方式统一为通用 ADB，旧 MuMu 配置自动兼容；这不代表已验证所有模拟器的游戏兼容性。
检测、测试、任务初始化和断线重连使用用户选择的路径和地址，GUI 重连不会跳到其他实例。
GUI 默认不启动模拟器；只有明确开启“自动启动模拟器”后，开始队列时才会尝试启动。
不会主动重启已有模拟器；仅在明确开启“全部任务完成后关闭模拟器”且满足成功条件时请求关闭指定 MuMu 实例。不更改 `config/device.json`。

首次连接请按本文开头的 [MuMu 15 从零配置](../gui/README.md#mumu-15-从零配置) 操作；已有 platform-tools 的用户也可选择其 ADB。设置会自动保存，下一次打开会恢复连接设置。

检测和测试使用后台线程，不执行游戏任务，也不启动、重启或关闭模拟器。
检测期间禁用开始按钮 和设置编辑；关闭窗口会等待当前检测完成，避免销毁运行中的线程。
离线演练时禁用检测和测试按钮。测试不要求已安装游戏，正式任务连接时仍会验证游戏包。
当前配置位于用户数据目录；旧项目的 `config/gui.json` 已被 Git 忽略。分发时两者都不能打包。
可参考 `config/gui.example.json`，无需把开发者的地址和磁盘路径改进源码。

任务定义全部位于 `task_registry.py`。GUI 共有 14 个主任务；“奖励领取”位于末尾，
左侧仅显示“奖励领取”主任务及齿轮，不展开子项。点击齿轮，在任务设置区域独立勾选
邮箱、每日任务；新配置均不勾选。主任务只控制启用，未选子项时明确提示并阻止开始。
好友邮件设置保留在邮箱选项下，仅启用邮箱子项后可修改；主任务及子项独立保存选择。
子项按邮箱 → 每日任务的顺序复用原入口，
集合去重，继续使用现有日志、HOME 恢复、SafeStop 和失败重试机制。
旧配置按原邮箱和每日任务选择迁移，并保留好友邮件设置；关闭记忆选择则下次均不勾选。
启动队列前只读验证可执行入口的 interface 声明和 Pipeline 节点；CLI 原入口不变。

| GUI 名称 | 正式入口 | Pipeline 文件（assets/resource/pipeline/） |
| --- | --- | --- |
| 启动 | QJNN_StartAndEnterHome | 10_startup.json |
| 签到 | QJNN_Signin | 30_signin.json |
| 搭配师联盟 | QJNN_AllianceDaily | 85_alliance_daily.json |
| 搭配竞技场 | QJNN_ArenaDaily | 86_arena.json |
| 搭配评选赛 | QJNN_CompetitionDaily | 87_competition.json |
| 织梦人学会 | QJNN_DreamweaverCommission | 84_dreamweaver_commission.json |
| 小屋 | QJNN_HomeDaily | 96_home_daily.json |
| 好友 | QJNN_FriendSendHeart | 89_friend_send_heart.json |
| 体力任务 | QJNN_StaminaDaily | 85_stamina_daily.json |
| 谜之屋 | QJNN_MysteryHouseDaily | 93_mystery_house.json |
| 世界巡游 | QJNN_WorldTour | 97_world_tour.json |
| 服装店 | QJNN_ClothesShopPurchase | 94_clothes_shop.json |
| 分解重构 | QJNN_DecomposeDaily | 95_decompose.json |
| 奖励领取 → 邮箱 | QJNN_Mail | 50_mail.json |
| 奖励领取 → 每日任务 | QJNN_DailyTaskClaim | 98_daily_task_claim.json |

以上可执行入口均与 `assets/interface.json` 交叉核对，而不是按名称推测。
启动节点独立进入首页，不使用包含签到的 `QJNN_StartAndSignin`。
联盟的 `agent/qjnn_alliance_daily.py` 已组织委托及图书馆；小屋的
`agent/qjnn_home_daily.py` 已组织生活点滴及随机拜访，因此 GUI 不额外提交这些内部任务。

“开始任务”流程：检查非空勾选 → 保存勾选 → 固定顺序快照 → 禁用重复开始 和编辑
→ QThread 验证入口 → `MaaTaskExecutor.prepare` → `tools.run_task.TaskRuntime`
→ 连接/确认指定设备，加载一次 Resource、注册一次扩展、创建一次 RecoveringController
和 Tasker → 逐项 `TaskRuntime.execute` → 复用既有 preflight、runtime override 和
`Tasker.post_task(真实入口)` → 等待结果 → 成功才执行下一项 → 清理后恢复开始按钮。
默认单项失败立即中断后续队列。设置 → 运行设置可开启“单项任务失败后继续后续任务”，
自动保存后在下一批生效。开启后失败项保留失败状态和用时，不重复执行该任务；通过已有返回箭头
识别和两帧校验恢复主页，确认主页工具栏后才运行下一项。取消/确认弹窗、第三方登录、
剧情自动播放等受保护页面或无法恢复主页时仍结束队列；连接/初始化失败、手动停止也不继续。
最后一项失败无需恢复；所有已勾选任务尝试结束后显示成功/失败数量，不显示“整个队列已完成”。
停止设置线程安全 Event，当前执行器退出后显示“已停止”。
运行中关闭窗口也先停止并等待后台退出，不销毁运行中的 QThread。
连接、加载和初始化失败时没有业务任务提交，界面显示具体错误。

`tools/run_task.py` 从原有 `run` 提取了 `TaskRuntime` 和 `_run_pipeline`，
GUI 和原 CLI 共用同一份初始化、任务提交及结果检查逻辑。原 CLI 参数保持不变；
CLI 使用原设备发现策略，GUI 显式启用严格地址选择。
同一批只建立一次环境，不为每项重建连接。原 RecoveringController 的故障恢复策略保留。
队列完成、失败或取消后，先 `Tasker.post_stop().wait()` 等待回调退出，再恢复错误截图默认值，
释放 Tasker、Controller、Resource 的 Python 引用，清理 ctypes 回调循环引用，
由当前 binding 的析构接口释放原生对象；整个流程在工作线程完成。
环境清理本身不会调用 adb disconnect、kill-server 或停止模拟器；全部任务成功后是否请求关闭 MuMu，由独立的“全部任务完成后关闭模拟器”设置控制。再次点击“开始任务”创建一批新环境。

停止按钮设置 `stop_event`。提交前检查取消；运行中通过
`Tasker.post_stop().wait()` 协作停止并等待任务回调退出，后续任务不再提交。
自定义回调和初始化阶段可能需要等待已有操作返回，不能保证瞬时停止。实机停止行为尚未验证。

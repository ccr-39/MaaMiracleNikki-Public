# 运行素材与公开边界

本说明对应 0.1.2-rc.1；此前历史数字不得用作当前统计。当前正式运行模板为 **22 张 PNG**。新增评选赛点赞图已计入，QQ 登录图标已从运行引用移除并保存在私有开发目录。

所有 22 张均已查看实际内容，均为局部识别素材，不含完整游戏画面或玩家账号信息。但小图、脱敏、运行必需不等于已获得再分发授权；当前没有证据可将这些模板列为已获准公开。开发源码、最新本地完整源码候选及完整 Windows 候选均保留 **22 张**；实际公开分发为 **0 张**，不得将这些完整候选上传到公开仓库或 Release。当前状态见[发布说明](PUBLIC_RELEASE_STATUS.md)。

| 相对 assets/resource/image 的路径 | 数量 | 用途与当前授权状态 |
| --- | ---: | --- |
| alliance/commission_back_arrow.png、top_back_arrow.png | 2 | 联盟返回控件；待确认 |
| arena/back_button.png | 1 | 竞技场返回控件；待确认 |
| common/chapter_back_arrow.png | 1 | 章节返回控件；待确认 |
| competition/home_back_button.png、judge_back_button.png、like_thumb.png | 3 | 评选赛返回、点赞识别；待确认 |
| daily/makeup_diamond.png、signin_claim.png、signin_entry.png、signin_page_marker.png | 4 | 补签钻石、签到领取、入口和页标题；待确认 |
| decompose/workshop_back_arrow.png | 1 | 设计工坊返回控件；待确认 |
| dreamweaver/missing_clothes_plus.png、purchase_plus_one.png | 2 | 缺衣入口、数量加一；待确认 |
| home_pages/current_theme/left_arrow.png、right_arrow.png | 2 | 主页翻页；原始来源仍待确认，不能仅因图形简单推定有权授权 |
| home_random_visit/loading_snowflake_01.png 至 03.png | 3 | 加载状态识别，代码动态构造文件名；待确认 |
| start/start_button.png、enter_game_button.png | 2 | 点击开始、进入游戏；待确认 |
| world_tour/event_collected_stamp.png | 1 | 巡游已收集标记；待确认 |

不分发：旧 QQ 登录图标、联盟脱敏全屏 fixture、织梦人数量测试裁剪、所有原始/调试截图、历史构建副本、userdata。原件保留在私有目录或既有私库历史，不进入新的公开 Git 历史。

可行替代路径：取得覆盖上述具体文件与 GitHub 源码/Windows 包分发方式的授权；或逐项使用不依赖游戏美术的 OCR、几何和颜色特征替换，并用离线样本验证原任务的保守行为。箭头可考虑程序绘制模板，但须验证识别效果；雪花、钻石、图章不能凭空删除而声称业务不受影响。替换实现完成前，不能公开含这些资源的包。

两张 GUI SVG 已独立重绘为基础折线，按项目 MIT 提供；GUI 截图不是运行依赖，未纳入公开准备目录。游戏 PNG、美术和商标不因项目采用 MIT 而获得授权。OCR 的独立来源和许可问题见[第三方说明](../THIRD_PARTY_NOTICES.md)。

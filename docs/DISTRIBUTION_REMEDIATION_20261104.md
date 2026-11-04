# 2026-11-04 分发整改

本文件覆盖此前审计中的候选包状态，旧报告保留作历史证据。本次只修改打包及许可材料，不修改任务逻辑。22 张正式局部模板保持原文件和调用。

## Windows 和视频插件

运行平台限定 Windows 10/11 x64，使用系统自带 Universal CRT，不随包复制来源尚未确定的 18 个 api-ms-win-* 和 ucrtbase.dll。Microsoft 官方说明 Windows 10/11 始终使用系统 UCRT，即使应用附带本地副本。其余 VC Runtime 使用 CPython 安装文件对应版本和原始许可，不扩大解释为任意 Microsoft DLL 可再分发。

隔离环境仅安装 packaging/requirements.txt 的固定依赖，避免全局 opencv-python 5.0.0.93 污染。MMN 没有调用 VideoCapture/VideoWriter，仅使用 OpenCV 的图像处理接口；打包时排除可选 opencv_videoio_ffmpeg*.dll。OpenCV 官方同版 readme 明确允许在不需要 FFmpeg 时从分发包排除此插件，图像功能继续可用。原始 DLL 未删除，旧包仍保留原有文件和未解决的分发限制。

旧两份 DLL 的 wheel RECORD、SHA256、原生字符串、OpenCV 固定 ffmpeg.cmake 和 opencv_3rdparty 构建脚本均已记录。它们使用 FFmpeg n7.1、静态库封装动态视频插件，配置包含 libvpx/libaom/OpenH264 支持且未开启 GPL 选项。旧包需要的源码安排不能靠删掉许可文字规避；本次新包不含两份 DLL，因此不对外分发这两份 FFmpeg 二进制。

## 源码与库替换

第三方原始许可证和版权文件保留。Qt/PySide/Shiboken 6.12.0、MaaFramework 5.14.0、固定子模块、MaaDeps 2.12.6 和 MPL ZeroMQ 4.3.5 与 Eigen 3.4.1 对应源码归档及补丁单独准备，清单为 licenses/corresponding-source-manifest.json。将来若发布，必须将对应源码附件与二进制同一次提供，并在 Release 显著链接；目前仍为本地材料，不将尚未上传的地址描述成已完成交付。

应用通过外部 DLL/PYD 动态使用 LGPL 库。允许为了调试修改库而进行必要逆向工程；可在备份后替换相同 ABI、Python ABI 和 x64 架构的库。源码副本提供应用 Python 文件、启动器、spec、固定依赖和重构建工具。原始完整性清单用于原包校验，不作为禁止运行修改版本的签名锁。

## 游戏素材

本地完整源码及候选包保留 22 张运行必需的局部模板，另含两张独立重绘 SVG。素材清单及既有审图证据复用，没有重新逐张审计或改为 OCR。游戏画面版权属于对应权利人；尚未获得覆盖这 22 个文件的源码仓库和 Windows ZIP 再分发许可。局部裁剪、免责声明、其他项目公开惯例均不构成授权。故本地完整候选仍禁止公开上传；实际公开游戏图片数量为 0。

## ONNX 整改结果

新候选使用 PaddlePaddle 官方 Apache-2.0 固定版本 inference.onnx 原文件，重命名但不转换或优化。官方下载 tar 中的文件与固定 Hugging Face 版本也逐字节匹配，记录见 licenses/distribution-evidence-20261104/official-tar-check.json。旧模型及未完整复原的导出链只留在原项目；新包不再依赖它们。12 组自绘文字离线对比通过，不将有限检查扩大为全部游戏页面的识别保证。

# OCR 模型目录

运行时需要 det.onnx、rec.onnx、keys.txt。模型与字典不提交 Git，请运行：

```powershell
powershell -ExecutionPolicy Bypass -File tools/setup_ocr.ps1
```

本轮完整候选采用 PaddlePaddle 官方固定版本 ONNX，许可证为 Apache-2.0。仅将官方 inference.onnx 重命名为 det.onnx 或 rec.onnx；MMN 不导出、不优化、不修改权重。具体仓库、提交和 SHA256 在 licenses/ocr/official-models-20261104.json。字典来源与提取记录见 licenses/ocr/keys-provenance-20261104.json。

安装器固定下载 URL、版本与全部三个文件的哈希。先下载验证到临时目录，再通过项目内 install/ocr-随机标识 事务目录替换，失败回滚，保留原目录附加文件。默认不会覆盖哈希不符的现有资源；确认修复时使用 -Force。出现未完成的 previous 备份应先恢复，不覆盖旧恢复点。

安装器不会自动覆盖本机已存在的旧模型；需要迁移时确认后显式使用 `-Force`。新旧模型的 12 组自绘文字离线对比一致，不代表完整游戏任务验收。当前公开源码及 Windows 包中的游戏模板仍未确认再分发授权，公开状态不改变其权属；见[素材说明](../../../../docs/ASSET_NOTICE.md)。

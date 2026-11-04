# 对应源码与库替换

分发最终 Windows 包时，应同次提供实际使用的第三方对应源码材料、原始通知、适用补丁和构建说明。完整清单见[13 份归档清单](../licenses/corresponding-source-manifest.json)，原始许可证见[附件目录](../licenses/README.md)。未创建的 Release 或上游通用主页不能作为已经履行交付的证明。

归档涵盖 PySide、Qt 模块、MaaFramework 与固定子模块、MaaDeps/vcpkg、ZeroMQ、Eigen。准确版本、URL、SHA256、大小和已核对的 port SHA512 均保留在清单中；MaaDeps/vcpkg 归档包含实际 ports、patches 和构建脚本。`prepare_distribution.py` 逐份校验并将 13 份复制到独立分发材料目录。

Qt、Maa 等库以外部 DLL/PYD 动态使用；允许为调试修改库而进行必要逆向工程。用户可备份后替换同 ABI、Python ABI 和 x64 架构的库，或利用应用源码、spec、固定依赖和构建工具重新组合。更新可能覆盖修改，应先备份。

`mmn-update.json` 校验原始包的完整性，不作为禁止运行本地修改库的签名锁。不能将通过原包哈希检查解释为完成修改库替换或全部游戏实机验收。

当前包不分发可选 FFmpeg 或旧兼容 UCRT；仍保留其他实际组件的原始声明。素材授权不由这些第三方源码材料替代，见[素材说明](ASSET_NOTICE.md)。

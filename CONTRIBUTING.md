# Contributing to SchedNav

欢迎提交可复现的问题、合同测试、数据适配器和文档修正。较大的模型、指标或动作空间变更，请先开 Issue 说明目标和验证方法。

## 本地开发

需要 Python 3.11+。从仓库根目录执行：

```sh
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -e .
python examples/quickstart.py
python -m unittest discover -s tests -v
python scripts/check_public_boundary.py
```

可选预测测试需要 `python -m pip install -e ".[forecast]"`；核心测试不需要 GPU、数据集下载或模型凭据。PowerShell 边界检查入口仍保留，并调用同一 Python 检查器。

## 提交 Pull Request

1. 从当前 `main` 建立工作分支，保持改动聚焦。
2. bug 修复附最小合成复现和定向回归测试；不要上传真实逐 Job 记录。
3. 说明行为变化、运行过的检查和仍未验证的部分；同步受影响文档。
4. 使用 PR 模板描述证据。维护者审核后合并；不保证响应时限。

## 数据与科研边界

- 原始 Trace、逐 Job 派生数据、运行日志、checkpoint、凭据和外部源码树不得提交。
- 合成 fixture 必须标明来源，不得冒充真实实验。
- 指标结论必须绑定 Trace、窗口、人口、策略和实现 fingerprint；不能跨不同研究直接排名。
- 保留历史证据字节。需要更正时写入新的版本和解释，不覆盖冻结回执。
- Agent 只选择声明的高层策略；不开放任意 shell、Job/Node/GPU placement。
- 第一方贡献遵循 MIT；引入第三方材料时同步更新许可声明。

完整开发约束见 [AGENTS.md](AGENTS.md)，文档入口见 [docs/README.md](docs/README.md)。漏洞请按 [SECURITY.md](SECURITY.md) 私下报告。

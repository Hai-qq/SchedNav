# Security policy

SchedNav 是研究软件，当前维护对象是 `main` 分支；尚无稳定发行版或长期支持版本。

## 报告漏洞

请使用 GitHub 的 [私密漏洞报告](https://github.com/Hai-qq/SchedNav/security/advisories/new)，不要先在公开 Issue 中粘贴凭据、可利用的部署信息或真实任务数据。

报告请包含受影响提交、最小合成复现、预期与实际行为、影响范围。维护者将确认问题并协调修复和披露，但不承诺固定响应时限。

## 运行边界

Host bridge 应仅用于可信本地研究环境。遵循 [集成说明](docs/agentteams-integration.md)配置身份验证、路径范围和 operation allowlist；不要将研究 bridge 直接暴露到公网。

密钥使用仓库外配置。公开回执只保留必要汇总，原始 Trace、逐 Job 派生数据、日志与运行环境留在仓库外。`scripts/check_public_boundary.py` 是有限模式检查，不能替代人工审查或证明不存在所有敏感信息。

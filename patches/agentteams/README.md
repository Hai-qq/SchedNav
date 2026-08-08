# AgentTeams compatibility patches

SchedNav 固定使用 AgentTeams `v1.2.1`（commit `552d0fb54d697b0689dafb6a01740e1a5f507552`），上游源码不进入 SchedNav 公开仓库。本目录只保存 SchedNav 为可复现部署维护的最小补丁。

`windows-appservice-parity.patch` 将同版本 `agentteams-install.sh` 已实现的 Matrix AppService 凭据生成、持久化和 Controller 透传逻辑补到 Windows `agentteams-install.ps1`。未应用时，当前 embedded Controller 会因缺少 AppService 凭据退出，Manager 无法创建。

在仓库根目录应用：

```powershell
git -C AgentTeams apply ..\patches\agentteams\windows-appservice-parity.patch
```

补丁不包含 API Key、管理员密码、令牌、AgentTeams 源码副本或运行数据。应用后应先运行 PowerShell 语法解析，再按 `docs/agentteams-integration.md` 的 runtime gate 验证。

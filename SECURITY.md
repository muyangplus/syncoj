# 安全策略

## 支持的版本

| 版本 | 接收漏洞报告 |
|---|---|
| `main` 上的最新状态 | ✅ |
| 更早的提交、已经发出去的旧包 | ❌（先升到最新再报） |

旧版本（含留在机房镜像里的旧 Agent）不修复、不回补；升级步骤见 [`docs/operate.md`](docs/operate.md)。

## 怎么报

1. 仓库 `Security` 标签页 → `Report a vulnerability`（GitHub 私密漏洞报告）。
2. 不开公开 issue、不发讨论区。

报告内容：

| 项 | 说明 |
|---|---|
| 组件与版本 | 服务端：`server/pyproject.toml` 的 `version`；Agent：状态目录里记录的版本 |
| 复现步骤 | 最小步骤与攻击面 |
| 验证情况 | 是否在自建环境验证、有无日志 |

回复在该私密报告内：确认、修复计划或不修复的原因；修复后再商定公开时间。

## 算漏洞的

- 绕过认证：Agent 的 token、六位配对码、管理员口令与会话
- 路径穿越：Agent 上报的路径、下发与扫描的路径模板、`judge_result/` 下的文件名
- 自更新链路：签名校验、发布包解包、回滚（[`docs/design/06-security.md`](docs/design/06-security.md)、
  [`ADR-0006`](docs/decisions/ADR-0006-delete-nothing-of-the-verifier.md)）
- 把选手代码、口令或密钥从服务端、机器上读走
- 拒绝服务：一个请求打垮单机服务端，或者一个包塞满磁盘

## 不算漏洞的

- **选手对自己的机器有 root**：本地篡改与侧信道不在防护范围（Agent 可被替换，见 [`docs/design/06-security.md`](docs/design/06-security.md)）；同步上来的代码作参考，不作证据；不做违规判定（[`docs/design/08-milestones-and-open.md`](docs/design/08-milestones-and-open.md)）。
- **zip 密码只挡随手翻看**：ZipCrypto，不是加密保护（[`ADR-0011`](docs/decisions/ADR-0011-zipcrypto-not-aes.md)）。
- 内网明文 HTTP 部署、管理端口暴露到公网：属部署选择；公开端口只放装机页与选手页（[`ADR-0005`](docs/decisions/ADR-0005-public-port.md)）。
- 单机 SQLite，无多副本、无高可用（v1 为单机内网部署）。

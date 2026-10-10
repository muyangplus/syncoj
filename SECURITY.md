# 安全策略

## 支持的版本

| 版本 | 接收漏洞报告 |
|---|---|
| `main` 上的最新状态 | ✅ |
| 更早的提交、已经发出去的旧包 | ❌（先升到最新再报） |

升级路径本身就是产品的一部分（[`docs/operate.md`](docs/operate.md)）；旧版本
（尤其是还留在机房镜像里的旧 Agent）不再修，也不承诺回补。

## 怎么报

**只用 GitHub 的私密漏洞报告**：仓库的 `Security` 标签页 → `Report a vulnerability`。
不要开公开 issue、不要发到讨论区 —— 这个仓库里有考试期间正在跑的通信协议、签名自更新
与选手代码回收，公开披露等于把所有还没考完的考场一起暴露。

报告里请带上：

- 受影响的是服务端还是 Agent、是什么版本（服务端看 `server/pyproject.toml` 里的
  version，Agent 看状态目录里记录的版本）
- 能复现的最小步骤，以及你认为的攻击面在哪
- 有没有在你自己搭的环境里验证过、有没有留下日志

我会在那条私密报告里回复：确认、修复计划，或者说明为什么不修。修完再一起商量
什么时候公开。

## 算漏洞的

- 绕过认证：Agent 的 token、六位配对码、管理员口令与会话
- 路径穿越：Agent 上报的路径、下发与扫描的路径模板、`judge_result/` 下的文件名
- 自更新链路：签名校验、发布包解包、回滚（[`docs/design/06-security.md`](docs/design/06-security.md)、
  [`ADR-0006`](docs/decisions/ADR-0006-delete-nothing-of-the-verifier.md)）
- 把选手代码、口令或密钥从服务端、机器上读走
- 拒绝服务：一个请求打垮单机服务端，或者一个包塞满磁盘

## 不算漏洞的

- **选手对自己的机器有 root。** Agent 跑在选手机器上、随时可能被替换
  （[`docs/design/06-security.md`](docs/design/06-security.md)），所以本地篡改与
  侧信道不在防护范围内：同步上来的代码只当"参考"，不当"证据"。考试期间也刻意
  不做违规判定（[`docs/design/08-milestones-and-open.md`](docs/design/08-milestones-and-open.md)）。
- **zip 密码只挡"随手翻看"。** 用的是 ZipCrypto，不是加密保护
  （[`ADR-0011`](docs/decisions/ADR-0011-zipcrypto-not-aes.md)）。
- 内网明文 HTTP 部署、把管理端口暴露到公网 —— 那是部署选择，不是产品缺陷；
  公开端口只放装机页与选手页的设计见
  [`ADR-0005`](docs/decisions/ADR-0005-public-port.md)。
- 服务端是单机 SQLite，没有多副本、没有高可用：v1 就是单机内网部署。

# 快速上手（教师）

## 教师上手流程

1. 初始化：

   ```bash
   syncoj-server init
   syncoj-server serve --host 0.0.0.0
   ```

2. 浏览器打开 `http://<服务端>:8000`，登录。
3. 「场次管理」→「新建场次」。可选：指定默认名单、填开考 / 结束时间。
4. 「名单库」→「新建名单」→「粘贴导入」（可直接从 Excel 粘贴）。
5. 「场次管理」→「设置」→ 选这份名单。
6. 「场次管理」→ 进入场次 →「应用名单」。
7. 「场次管理」→「题目」→ 登记题目。标识用作目录名与代码文件名；「代码路径」不确定时留空。
8. 装 Agent（→ 见 [`install-agent.md`](install-agent.md)）：

   ```bash
   syncoj-server bootstrap-key issue --out /etc/syncoj/bootstrap.key
   ```

   把密钥放进镜像（root 只读）→ 机器开机。
9. 「机器配对」→ 按机器桌面上的六位配对码，认领到名单条目。
10. 收成绩：外部评测器（LemonLime / Arbiter）输出到 `judge_result/`（→ 见 [`reference/judge-result.md`](reference/judge-result.md)），或「成绩」→「自带评测」（→ 见 [`operate.md`](operate.md)「自带评测」）。

命令行等价流程 → 见 [`operate.md`](operate.md)「命令行等价流程」。

## 配对

| 情况 | 是否要重新配对 |
|---|---|
| 换场次（新场次已应用同一份名单） | 否 |
| 系统重装、快照还原、升级 Agent | 否 |
| 换人（改派 / 解绑）、删除名单条目 | 是 |

配对对象是名单条目（人），不是某场次的选手 → 见 [`design/04-server.md §4.6`](design/04-server.md)。

## 两个端口

| 端口 | 内容 | 使用者 |
|---|---|---|
| `serve --port`（默认 **8000**） | 管理界面、全部 API、`/docs` | 教师 |
| `SYNCOJ_PUBLIC_PORT`（默认 **80**） | 仅 `/install` 与 `/player`；管理端、`/docs`、`/openapi.json` 均 404 | 考场机器（地址 `http://<服务端>/`） |

| 设置 | 效果 |
|---|---|
| `SYNCOJ_PUBLIC_PORT=0` | 关闭公开端口 |
| `SYNCOJ_PUBLIC_PORT=`（空） | 视为未设，用 80 |
| 专用账号绑不上 80 | 公开端口降级关闭，管理端口照常；修法二选一：`AmbientCapabilities=CAP_NET_BIND_SERVICE`，或 `SYNCOJ_PUBLIC_PORT=8080` 等非特权端口 |

→ 见 [`design/05-api-and-frontend.md §5.7`](design/05-api-and-frontend.md)。

## 目录约定

| 项 | 默认路径 |
|---|---|
| 机器上的代码 | `桌面/<准考证号>/<题目名>/<题目名>.cpp` |
| 服务端回收落点 | `source/<场次>/<准考证号>/<题目名>/<题目名>.cpp` |
| 题面 / 样例 | 桌面根目录，zip 原样落盘（服务端不解压） |

下发落点、回收目录、归题规则均可配置 → 见 [`design/04-server.md §4.5`](design/04-server.md)；装机侧见 [`install-agent.md`](install-agent.md)「目录约定」。

## 题面加密码

1. 「文件下发」→ 对 `.zip` 点「密码」：服务端就地加密 / 改密码，并生成或更新 `password.txt`。
2. 把 `password.txt` 与 zip 一起下发。

→ 见 [`design/05-api-and-frontend.md §5.5`](design/05-api-and-frontend.md)。

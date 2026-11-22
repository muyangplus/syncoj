# 快速上手（教师）

## 管理端导航

| 侧栏分组 | 页面 |
|---|---|
| 总览 | 工作台 |
| 考务 | 场次管理、名单库、选手状态 |
| 机器 | 机器配对、Agent 发布 |
| 代码与评测 | 代码台账、文件下发、评测、成绩 |
| 系统 | 审计日志 |

顶栏「快捷操作」菜单与 Ctrl/⌘+K 命令面板：召集机器、按码配对、装机命令、新建场次、下发文件、评测全部题目、重扫成绩、导出成绩 CSV。

## 开考流程

1. 启动服务端：

   ```bash
   syncoj-server init
   syncoj-server serve --host 0.0.0.0
   ```

   预期：`init` 打印 `[+] 已创建管理员: admin` 与 `初始口令: …`（只显示一次）；`serve` 打印 `[i] 考试机页面: http://…/   （公开端口 80；管理界面在 8000）`。
2. 浏览器打开 `http://<服务端>:8000`，以 `admin` 登录。预期：进入「工作台」。
3. 「场次管理」→「新建场次」（或顶栏「快捷操作」→「新建场次」）。可选：指定默认名单、填开考 / 结束时间。预期：列表出现新场次，「当前」列显示「使用中」。
4. 「名单库」→「新建名单」→ 选中该名单 →「粘贴导入」（可直接从 Excel 粘贴）。预期：名单下方显示「共 N 人」。
5. 把名单应用到场次，二选一：
   - 「名单库」→ 选中名单 →「应用到「<当前场次>」」；
   - 「场次管理」→ 该行「设置」→「默认名单」选这份名单 →「按这份名单补人」（只补人、不删人）。

   预期：「选手状态」列出名单里的选手。
6. 「场次管理」→ 该行「题目」→ 登记题目。标识用作目录名与代码文件名；「代码路径」不确定时留空。
   已有 LemonLime / HOJ / Hydro 等题目包时：「评测」→「题目」卡片 →「导入题目配置」（→ 见 [`operate.md`](operate.md)「导入导出题目配置」）。
7. 装 Agent（→ 见 [`install-agent.md`](install-agent.md)）：

   ```bash
   syncoj-server bootstrap-key issue --out /etc/syncoj/bootstrap.key
   ```

   把密钥放进镜像（root 只读）→ 机器开机。预期：每台机器桌面出现「配对码.txt」。
8. 「机器配对」→ 待配对机器行「配对」，或「按码配对」输入机器桌面上的六位配对码 → 选名单条目。预期：机器桌面上的「配对码.txt」消失；「选手状态」该选手「最后心跳」开始刷新。
9. 收成绩，二选一：
   - 外部评测器（LemonLime / Arbiter）输出到 `judge_result/`（→ 见 [`reference/judge-result.md`](reference/judge-result.md)）；
   - 自带评测：「评测」页（→ 见 [`operate.md`](operate.md)「自带评测」）。

   预期：「成绩」矩阵出现分数；自带评测的格子可点开看测试点明细。

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

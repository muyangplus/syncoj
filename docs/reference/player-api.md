# 考生页接口（`/api/v1/player`）

考生页（前端 `/player`，标题「考生须知」）用的四个接口。都**不要管理端凭据**，公开端口（默认 80）与管理端口上都可用。
实现：`server/syncoj_server/api/player.py`；设计取舍见 [`design/05-api-and-frontend.md §5.6`](../design/05-api-and-frontend.md)。

## 1. 身份从哪来

| 来源 | 怎么带 | `matched_by` | 能看到 |
|---|---|---|---|
| 无（降级预览） | — | — | 当前在考的场次、时间窗、全场公告 |
| 本机自动匹配 | 不带令牌；服务端按**来源 IP** 认机器，再走 `resolve_machine` | `machine` | 本人的完整上下文、下载 |
| 准考证号 + 考生页密码 | `POST /login` 换令牌，之后带 `X-Player-Token` 头 | `token` | 同上 |

- 考生页密码是**场次级**的：「场次管理」→「设置」→「考生页密码」。只写不读，接口只回 `has_player_password`。没设 = 这条登录路径关着。
- 令牌：`v1.<contest_id>.<player_id>.<过期 Unix 秒>.<HMAC-SHA256>`，有效 8 小时。签名里含场次当前密码哈希的指纹：**换密码或清除密码，已签出的令牌全部作废**。密钥在 `<data_root>/player-token.key`（首次登录时生成，0600）。
- 令牌走自定义头 `X-Player-Token`，不用 `Authorization`；失效回 **403** `player_token_invalid`（不是 401，401 在前端表示管理员会话过期）。

## 2. 接口

### `GET /preview` —— 降级预览

```json
{
  "contests": [
    {
      "slug": "2026-mock",
      "name": "校内模拟赛",
      "status": "running",
      "starts_at": "2026-11-22T01:00:00Z",
      "ends_at": "2026-11-22T05:00:00Z",
      "notice": {"filename": "NOTICE.md", "content": "# 考场须知\n…"},
      "login_enabled": true
    }
  ],
  "server_time": 1795309200
}
```

| 规则 | 说明 |
|---|---|
| 哪些场次 | 状态 `running` / `frozen`，且没到 `ends_at`（还没开考的也列，页面显示「距开考」）；其余状态一律不算在考 |
| 顺序 | 没配开考时间的在前，其余按 `starts_at` 升序；最多 20 场 |
| `notice` | 只取**目标是全场**（`target_kind = "all"`）且未取消的 `NOTICE.md` 下发，多份取最后下发的；只发给分组 / 指定选手的公告不出现 |
| 绝不出现 | 任何选手的考号、姓名、座位、分组、个人下发清单 |

### `POST /login` —— 准考证号 + 密码换令牌

请求：`{"contest": "<slug>", "player_no": "S001", "password": "…"}`；响应：`{"token": "v1.…", "expires_at": 1795338000}`。

| 情况 | 状态码 | `code` |
|---|---|---|
| 成功 | 200 | — |
| 考号不存在 **或** 密码错（不区分） | 403 | `player_login_failed` |
| 场次没设考生页密码 | 409 | `player_login_disabled` |
| 场次标识不存在 | 404 | `not_found` |
| 同一 IP 超过 `player_lookup_per_ip_per_minute`（默认 30 次 / 分钟） | 429 | `rate_limited`（`details.retry_after`） |

考号**精确匹配**（只去首尾空白，大小写敏感）。

### `GET /context` —— 本人的完整上下文

- 带 `X-Player-Token` → 令牌身份；不带 → 本机自动匹配。
- 自动匹配失败：404 `not_found`（机器没注册）/ 403 `pairing_required`、`no_active_contest` / 409 `ambiguous_contest`、`contest_missing`、`contest_player_missing`。页面据此退到降级预览。
- 响应：`contest`、`player`（考号 / 姓名 / 座位 / 分组）、`notice`（发给本人的 `NOTICE.md`）、`assets`（发给本人的下发台账，每行带资产 `id`）、`matched_by`、`server_time`。不含代码、成绩、其他选手。

旧的 `?contest=&player_no=` 免密码查找已删除：参数被忽略，等同于本机自动匹配。

### `GET /assets/{asset_id}` —— 网页下载

| 情况 | 状态码 |
|---|---|
| 令牌有效或本机匹配成功，且该资产经未取消的下发任务、未取消的目标发给了本人 | 200，流式附件 |
| 没有令牌且认不出本机；令牌失效 | 403 |
| 资产不存在、不在本人所在场次、或本场根本没下发过 | 404 |
| 下发过，但不是发给本人（或已取消） | 403 |
| blob 已被清理 | 410 |

附件名取资产文件名，控制字符替换为 `_`，非 ASCII 走 RFC 5987 `filename*`。

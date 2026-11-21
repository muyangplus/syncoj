## 2. 核心协议：`tick`

全系统只有这一个关键接口。**服务端持有全部状态，Agent 近乎无状态。**
完整字段规范见 [`docs/reference/protocol.md`](../reference/protocol.md)。

```jsonc
// POST /api/v1/agent/tick   (Authorization: Bearer <token>)
// 请求
{
  "agent_version": "0.1.0",
  "machine_id": "b3f1...",              // /etc/machine-id 派生
  "ts": 1767225600,
  "scan_root": "/home/student/桌面",
  "scan": [                             // 本周期全量扫描索引，不含内容
    {"path": "main.cpp", "sha256": "ab12...", "size": 1234, "mtime": 1767225500}
  ],
  "deploy_progress": [{"target_id": 42, "bytes_done": 3145728}],  // 本机分片进度
  "deploy_done": [{"target_id": 7, "sha256": "cd34..."}],          // 已落地（带内容指纹）
  "deploy_failed": [],                                             // 本轮没下成的与原因
  "stats": {"disk_free": 10737418240, "last_error": null, "queue": 0}
}

// 响应
{
  "server_time": 1767225600,
  "next_tick_seconds": 20,              // 自适应：有活 2s，空闲 30s（管理端可改）
  "need_upload": ["main.cpp"],          // 服务端没有 / 版本落后的文件
  "deploy_jobs": [
    {"target_id": 42, "asset_id": 42, "url": "/api/v1/agent/assets/42",
     "sha256": "cd34...", "size": 10485760,
     "dest": "exam/testdata.zip"}      // 续传位置只看本机分片，不由服务端给
  ],
  "upgrade": null,                      // 或 {version, url, sha256, sig}
  "diagnostics_request": false,         // 一次性：教师点过「要一份」就回一次 true
  "config": {"scan_interval": 30, "max_file_size": 2097152}
}
```

**`diagnostics_request` 是一次性字段**：教师在机器行上点「诊断」后，**下一次** tick 响应里为
`true`，服务端同时清掉标记；没收到就再点一次，服务端不重发 —— 少收一份诊断包代价很小
（与卸载令牌"多轮原样重发"刻意不同）。正常与未配对两条 tick 路径都处理它。

每次 tick 上报全量索引：50 文件 × ~100 字节 ≈ **5KB/次**，成本可忽略。收益：

| 收益 | 说明 |
|---|---|
| 完整文件台账 | 谁有什么、何时变更、什么版本 |
| 删除检测 | 上一轮有、这一轮没有 = 选手删了 → 记审计事件 |
| 比 inotify 可靠 | 比对快照而非事件流，Agent 停机期间的变化不丢 |
| 不受 inotify 限额 | 无需调 `fs.inotify.max_user_watches`（Ubuntu 20.04 默认 8192） |

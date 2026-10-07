## 2. 核心协议：`tick`

整个系统只有一个关键接口。**服务端持有全部状态，Agent 零状态。**
完整字段规范见 [`docs/reference/protocol.md`](../reference/protocol.md)。

```jsonc
// POST /api/v1/agent/tick   (Authorization: Bearer <token>)
// 请求
{
  "agent_version": "0.1.0",
  "machine_id": "b3f1...",              // /etc/machine-id 派生
  "ts": 1767225600,
  "scan_root": "/home/student/桌面",
  "scan": [                             // 本周期全量扫描的索引，不含内容
    {"path": "main.cpp", "sha256": "ab12...", "size": 1234, "mtime": 1767225500}
  ],
  "partials": [{"asset_id": 42, "bytes_done": 3145728}],  // 未完成的下载
  "stats": {"disk_free": 10737418240, "last_error": null, "queue": 0}
}

// 响应
{
  "server_time": 1767225600,
  "next_tick_seconds": 20,              // 自适应：有活 2s，空闲 30s（管理端可改）
  "need_upload": ["main.cpp"],          // 服务端没有 / 版本落后的文件
  "deploy_jobs": [
    {"asset_id": 42, "url": "/api/v1/agent/assets/42",
     "sha256": "cd34...", "size": 10485760,
     "dest": "exam/testdata.zip", "offset": 3145728}   // 断点续传位置
  ],
  "cancel_assets": [],
  "upgrade": null,                      // 或 {version, url, sha256, sig}
  "config": {"scan_interval": 30, "max_file_size": 2097152}
}
```

每次 tick 上报全量索引：50 个文件 × ~100 字节 = **5KB/次**，20s 一次，带宽与 CPU 成本可忽略。收益：

| 收益 | 说明 |
|---|---|
| 免费得到完整文件台账 | 谁有什么、何时变更、什么版本 |
| 删除检测免费 | 上一轮有、这一轮没了 = 选手删了 → 记审计事件 |
| 比 inotify 更可靠 | Agent 停机期间的事件不会丢：比对的是快照而非事件流 |
| 不依赖 inotify 限额 | 无需调优 `fs.inotify.max_user_watches`（Ubuntu 20.04 默认仅 8192） |

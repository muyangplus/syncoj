## 1. 架构总览

```
┌──────────── 教师 ────────────┐
│  Vue3 SPA  ──HTTPS──> uvicorn│
│  （由服务端 StaticFiles 托管，│
│    不额外引入 Caddy/nginx）   │
└──────────────┬───────────────┘
               │
┌──────────────▼──────────────────────────────────┐
│  SyncOJ Server (uvicorn, 单进程)                │
│   API:  /api/v1/admin/*   /api/v1/agent/*       │
│   内存: AgentRegistry(在线状态，读路径)          │
│   后台: 状态批量落库 / 结果扫描                  │
│   SQLite WAL + blobs/(内容寻址) + source/       │
└──────────────┬──────────────────────────────────┘
               │  HTTP 轮询 (2s ~ 60s 自适应)
┌──────────────▼──────────────────────────────────┐
│  SyncOJ Agent  (systemd, Python 3.8, 零依赖)    │
│   扫描 → 上传 → tick → 下载 → 自更新             │
└─────────────────────────────────────────────────┘

LemonLime / Arbiter (GUI, 教师手动操作)
   读 source/<contest>/<player_no>/...  →  写 judge_result/  →  服务端扫描回写
```

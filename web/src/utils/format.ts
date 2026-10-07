/** 展示层的格式化工具。保持纯函数，便于单测与复用。 */

/**
 * 显示时区相对 UTC 的偏移**分钟数**。默认 +480（UTC+8）。
 *
 * 为什么不是浏览器时区：服务端给的所有时间都是带 `Z` 的 UTC，原先用
 * `new Date(...)` 之后取本地字段，于是教师从不在东八区的机器上打开管理界面时
 * 整页时间差几个小时，而页面上完全看不出来。
 *
 * 值由服务端下发（`GET /api/v1/meta`），见 `applyMeta()`。前端**不自己猜**：
 * 服务端与前端必须看同一只钟，猜出来的第二个真相源迟早会分叉。
 */
let displayOffsetMinutes = 8 * 60

/** 服务端下发的显示时区标识，仅用于展示"现在按哪个时区显示"。 */
let displayTimezoneLabel = '+08:00'

/** 由应用启动时调用一次（`main.ts`），把服务端的配置装进来。 */
export function applyMeta(offsetMinutes: number | null | undefined, label?: string | null): void {
  if (typeof offsetMinutes === 'number' && Number.isFinite(offsetMinutes)) {
    displayOffsetMinutes = offsetMinutes
  }
  if (label) displayTimezoneLabel = label
}

export function displayOffset(): number {
  return displayOffsetMinutes
}

export function displayTimezone(): string {
  return displayTimezoneLabel
}

/** 把偏移写成 `+08:00`，给界面上那句"按 XX 显示"用。 */
export function displayOffsetLabel(): string {
  const sign = displayOffsetMinutes >= 0 ? '+' : '-'
  const total = Math.abs(displayOffsetMinutes)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${sign}${pad(Math.floor(total / 60))}:${pad(total % 60)}`
}

/**
 * 服务端返回的是 ISO8601（UTC，带 Z）。按**显示时区**渲染。
 *
 * 实现方式是把偏移加进去、再用 `getUTC*` 取字段 —— 这样浏览器的时区与
 * 夏令时规则完全不参与，页面上那个钟点只由服务端下发的配置决定。
 */
export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return String(iso)
  const shifted = new Date(date.getTime() + displayOffsetMinutes * 60 * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())} ` +
    `${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())}:${pad(shifted.getUTCSeconds())}`
  )
}

/** 相对时间。心跳场景下"12 秒前"比绝对时间有用得多。 */
export function formatSince(
  seconds: number | null | undefined,
  fallback = '从未',
): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return fallback
  const value = Math.max(0, Math.round(seconds))
  if (value < 60) return `${value} 秒前`
  if (value < 3600) return `${Math.floor(value / 60)} 分钟前`
  if (value < 86400) return `${Math.floor(value / 3600)} 小时前`
  return `${Math.floor(value / 86400)} 天前`
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return '—'
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes / 1024
  let index = 0
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  return `${value.toFixed(value >= 100 ? 0 : 1)} ${units[index]}`
}

/** 进度百分比。分母为 0 时返回 0 而不是 NaN —— NaN 会让 el-progress 整条不渲染。 */
export function percent(done: number, total: number): number {
  if (!total || total <= 0) return 0
  return Math.min(100, Math.round((done / total) * 100))
}

/**
 * 审计事件的分类名转成中文。保留未知分类原文，别让它消失。
 *
 * 这份表覆盖**服务端实际会写的全部 category**（扫 `server/` 与 `agent/` 里
 * 所有 `category=` 字面量得到）。漏一个的后果不是报错，而是界面上一列里
 * 突然出现几个英文单词 —— 那种东西没人会去补，只会一直留在那里。
 */
const EVENT_CATEGORY_LABELS: Record<string, string> = {
  // 注册与配对
  enroll: '注册',
  enroll_conflict: '注册冲突（指纹撞在线机器）',
  enroll_ambiguous: '注册歧义（多台共用指纹）',
  bind: '机器配对',
  agent_rebind: '机器改派',
  agent_unbind: '解除绑定',
  agent_revoke: '作废机器',
  agent_uninstall: '卸载机器',
  bootstrap_key: '统一密钥',
  machines_clear: '清空待配对机器',
  // 场次与选手
  contest_delete: '删除场次',
  roster_apply: '应用名单',
  player_delete: '删除选手',
  players_clear: '清空选手',
  // 代码台账
  files_clear: '清理台账',
  file_deleted: '文件消失',
  file_restored: '文件恢复',
  // 下发与题目
  deploy_created: '下发创建',
  deploy_cancelled: '下发取消',
  deploy_delete: '删除下发任务',
  deploy_failed: '下发失败',
  problem_import: '题目登记',
  problem_clear: '清空题目',
  // 成绩
  judge_manual: '手工录分',
  scores_clear: '清空成绩',
  // 审计
  events_clear: '清空日志',
  // Agent 侧上报
  bulk_rewrite: '批量重写',
  scan_rejected: '扫描拒绝',
  scan_incomplete: '扫描不完整',
  scan_no_match: '不符合题目预设',
  oversize_skipped: '超限跳过',
  upload_rejected: '上传被拒',
  offline: '离线',
  disk_full: '磁盘满',
  // 诊断包
  diagnostics_request: '要一份诊断',
  diagnostics_received: '收到诊断包',
  // 发布
  release_uploaded: '发布上传',
  release_rollout: '发布铺开',
  release_yank: '发布撤回',
}

export function eventCategoryLabel(category: string): string {
  return EVENT_CATEGORY_LABELS[category] ?? category
}

/** 审计日志的级别选择项。值跟着服务端 `EventLog.level` 走。 */
const EVENT_LEVEL_LABELS: Record<string, string> = {
  info: '信息',
  warning: '警告',
  error: '错误',
}

export function eventLevelLabel(level: string): string {
  return EVENT_LEVEL_LABELS[level] ?? level
}

const CONTEST_STATUS_LABELS: Record<string, string> = {
  draft: '筹备中',
  running: '进行中',
  frozen: '已封榜',
  closed: '已结束',
}

export function contestStatusLabel(status: string): string {
  return CONTEST_STATUS_LABELS[status] ?? status
}

export function contestStatusType(status: string): 'info' | 'success' | 'warning' | 'danger' {
  if (status === 'running') return 'success'
  if (status === 'frozen') return 'warning'
  if (status === 'closed') return 'info'
  return 'info'
}

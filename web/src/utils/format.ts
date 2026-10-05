/** 展示层的格式化工具。保持纯函数，便于单测与复用。 */

/** 服务端返回的是 ISO8601（UTC，带 Z）。转成本地时间显示。 */
export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return String(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
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

const EVENT_LEVEL_LABELS: Record<string, string> = {
  info: '信息',
  warning: '警告',
  error: '错误',
}

export function eventLevelLabel(level: string): string {
  return EVENT_LEVEL_LABELS[level] ?? level
}

/** 审计事件的分类名转成中文。保留未知分类原文，别让它消失。 */
const EVENT_CATEGORY_LABELS: Record<string, string> = {
  enroll: '注册',
  deploy_created: '下发创建',
  deploy_cancelled: '下发取消',
  deploy_failed: '下发失败',
  release_uploaded: '发布上传',
  release_rollout: '发布铺开',
  release_yank: '发布撤回',
  judge_manual: '手工录分',
  file_deleted: '文件消失',
  file_restored: '文件恢复',
  bulk_rewrite: '批量重写',
  scan_rejected: '扫描拒绝',
  scan_incomplete: '扫描不完整',
  oversize_skipped: '超限跳过',
  upload_rejected: '上传被拒',
  offline: '离线',
  disk_full: '磁盘满',
}

export function eventCategoryLabel(category: string): string {
  return EVENT_CATEGORY_LABELS[category] ?? category
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

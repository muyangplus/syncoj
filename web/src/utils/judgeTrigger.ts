/**
 * 评测任务的触发方式（§12.9、§12.12）：标签与筛选项。智能模式的任务 `trigger = smart`，
 * 具体信号在 `reason` 里。
 */

const TRIGGER_LABEL: Record<string, string> = { auto: '自动', manual: '手动', rejudge: '历史版本', smart: '智能' }

/** 智能模式各信号的短标签 */
export const SMART_REASON_LABEL: Record<string, string> = {
  moved_on: '换题',
  long_stable: '空闲',
  near_end: '将结束',
  offline: '掉线',
  contest_end: '已结束',
}

/** 一条任务的触发方式标签：智能模式显示信号 */
export function triggerLabel(trigger: string, reason?: string | null): string {
  if (trigger === 'smart' && reason) return `智能·${SMART_REASON_LABEL[reason] ?? reason}`
  return TRIGGER_LABEL[trigger] ?? trigger
}

/** 评测记录的触发方式筛选项；`smart:<信号>` 拆成 `trigger` + `reason` 两个参数 */
export const TRIGGER_FILTERS: { label: string; value: string }[] = [
  { label: '全部触发方式', value: 'all' },
  { label: '自动', value: 'auto' },
  { label: '智能', value: 'smart' },
  ...Object.entries(SMART_REASON_LABEL).map(([value, label]) => ({ label: `智能·${label}`, value: `smart:${value}` })),
  { label: '手动', value: 'manual' },
  { label: '历史版本', value: 'rejudge' },
]

export function triggerParams(value: string): { trigger?: string; reason?: string } {
  if (value === 'all') return {}
  const [trigger, reason] = value.split(':')
  return reason ? { trigger, reason } : { trigger }
}

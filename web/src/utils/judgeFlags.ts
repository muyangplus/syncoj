/**
 * 评测结果的风险标注（§12.11）。
 *
 * 分级在服务端（`judging/precheck.py` 的 `level_of`）：列表行只带 `warning_count` / `risk_count`，
 * 明细里每条带 `level`。这里只决定"怎么画"，不维护 code 表。
 */

export interface FlagCounts {
  warning_count?: number | null
  risk_count?: number | null
}

/** 评测记录的筛选项：`all` = 不筛（el-select 把空串当成"没选"，显示占位符） */
export const FLAG_FILTERS = [
  { value: 'all', label: '全部标注' },
  { value: 'warning', label: '有疑似违规' },
  { value: 'risk', label: '有风险' },
] as const

/** 筛选值 → 接口参数 */
export function flagParam(value: string): string | undefined {
  return value === 'all' ? undefined : value
}

/** 行的左边框：风险优先 */
export function flagRowClass(row: FlagCounts): string {
  if (row.risk_count) return 'sj-row-risk'
  if (row.warning_count) return 'sj-row-warning'
  return ''
}

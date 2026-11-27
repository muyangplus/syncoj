/**
 * 评测状态词表（§12.16）。说明文案是用户给定的原文，界面原样使用（优先于 ADR-0004 的「不写解释句」）。
 *
 * 标签显示代号（如 `PA`），悬停显示「名称：说明」。测试点结果、整题结果与任务过程状态都从这里取。
 */

export type StatusTone = 'success' | 'warning' | 'danger' | 'info' | 'primary'

export interface JudgeStatus {
  code: string
  name: string
  /** 中文短名（进度条、计数用） */
  label: string
  description: string
  tone: StatusTone
}

const LIST: JudgeStatus[] = [
  { code: 'PD', name: 'Pending', label: '排队', description: '您的解答正在排队等待评测中，请等待结果...', tone: 'info' },
  { code: 'SF', name: 'Submitted Failed', label: '提交失败', description: '您的此次提交失败，请点击按钮重新提交...', tone: 'danger' },
  { code: 'CP', name: 'Compiling', label: '编译中', description: '正在对您的源代码进行编译中，请等待结果...', tone: 'primary' },
  { code: 'JG', name: 'Judging', label: '评测中', description: '正在使用测试数据运行您的程序中，请等待结果...', tone: 'primary' },
  { code: 'CE', name: 'Compile Error', label: '编译错误', description: '无法编译您的源代码，点击链接查看编译器的输出。', tone: 'info' },
  { code: 'PE', name: 'Presentation Error', label: '格式错误', description: '您提交的代码已经很接近正确答案，请检查代码格式输出是否有多余空格，换行等空白符。', tone: 'warning' },
  { code: 'PA', name: 'Partial Accepted', label: '部分正确', description: '加油！您提交的代码通过了部分测试点，请考虑其他可能性。', tone: 'warning' },
  { code: 'AC', name: 'Accepted', label: '正确', description: '恭喜! 您的解题方法是正确的。', tone: 'success' },
  { code: 'WA', name: 'Wrong Answer', label: '答案错误', description: '您的程序输出结果与判题程序的答案不符。', tone: 'danger' },
  { code: 'RE', name: 'Runtime Error', label: '运行错误', description: '您的程序异常终止，可能的原因是：段错误，被零除或用非0的代码退出程序。', tone: 'danger' },
  { code: 'TLE', name: 'Time Limit Exceeded', label: '超时', description: '您的程序运行时间已超出题目限制。', tone: 'warning' },
  { code: 'MLE', name: 'Memory Limit Exceeded', label: '超内存', description: '您的程序实际使用的内存已超出题目限制。', tone: 'warning' },
  { code: 'OLE', name: 'Output Limit Exceeded', label: '输出超限', description: '您的程序输出超出限制。', tone: 'warning' },
  { code: 'SE', name: 'System Error', label: '系统错误', description: '糟糕，判题机系统出了问题。请报告给管理员。', tone: 'info' },
  { code: 'CA', name: 'Cancelled', label: '已取消', description: '您的此次提交被取消！', tone: 'info' },
]

export const JUDGE_STATUSES: Record<string, JudgeStatus> = Object.fromEntries(LIST.map((s) => [s.code, s]))

/** 外部评测器的别名 → 代号（与服务端扫描器同一张表） */
const ALIASES: Record<string, string> = {
  pc: 'PA',
  partial: 'PA',
  partiallycorrect: 'PA',
  partialaccepted: 'PA',
  presentationerror: 'PE',
}

export function judgeStatus(code: string | null | undefined): JudgeStatus | null {
  if (!code) return null
  const exact = JUDGE_STATUSES[code] ?? JUDGE_STATUSES[code.toUpperCase()]
  if (exact) return exact
  const alias = ALIASES[code.replace(/[\s_-]+/g, '').toLowerCase()]
  return alias ? JUDGE_STATUSES[alias] : null
}

/** el-tag 的 type；不认识的状态为 info */
export function statusTone(code: string | null | undefined): StatusTone {
  return judgeStatus(code)?.tone ?? 'info'
}

/** 悬停文字：「名称：说明」；不认识的状态原样返回 */
export function statusTooltip(code: string | null | undefined): string {
  const status = judgeStatus(code)
  return status ? `${status.name}：${status.description}` : code ?? ''
}

/** 显示用的代号（别名规整成代号） */
export function statusCode(code: string | null | undefined): string {
  return judgeStatus(code)?.code ?? code ?? '—'
}

/**
 * 评测任务的过程状态：排队 PD、编译中 CP、评测中 JG（旧评测机不报阶段时也算 JG）、
 * 失败 SF / SE、取消 CA；完成的任务用结果代号。
 */
export function taskStatusCode(task: { status: string; phase?: string | null; outcome?: string | null }): string {
  switch (task.status) {
    case 'queued':
      return 'PD'
    case 'running':
      return task.phase === 'compiling' ? 'CP' : 'JG'
    case 'failed':
      return task.outcome === 'SE' ? 'SE' : 'SF'
    case 'cancelled':
      return 'CA'
    default:
      return task.outcome || ''
  }
}

export const PROBLEM_TYPES = [
  { value: 'traditional', label: '传统' },
  { value: 'grader', label: '函数交互' },
  { value: 'answer_only', label: '提交答案' },
  { value: 'interactive', label: '交互' },
] as const

export type ProblemType = (typeof PROBLEM_TYPES)[number]['value']

export function problemTypeLabel(value: string | null | undefined): string {
  return PROBLEM_TYPES.find((t) => t.value === value)?.label ?? '传统'
}

export const COMPARE_MODES = [
  { value: 'strict', label: '严格', short: '严格' },
  { value: 'trailing_spaces', label: '忽略行末空格', short: '行末空格' },
  { value: 'trailing_newlines', label: '忽略文末空行', short: '文末空行' },
  { value: 'both', label: '两者都忽略', short: '默认' },
] as const

export type CompareMode = (typeof COMPARE_MODES)[number]['value']

export function compareModeShort(value: string | null | undefined): string {
  return COMPARE_MODES.find((m) => m.value === value)?.short ?? '默认'
}

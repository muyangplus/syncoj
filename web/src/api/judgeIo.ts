/**
 * 题目评测配置的导入 / 导出，以及数据包原样下载。
 *
 * 导入两步：`importPreview` 上传并拿到 token 与每题预览；`importApply` 只回传每题的
 * ident 与动作（新建 / 覆盖 / 跳过），配置由服务端凭 token 重新解析。
 */

import { downloadWithServerName, request } from './client'
import { paths } from './endpoints'
import type { operations } from './schema'
import type {
  JudgeExportPreviewOut,
  JudgeImportApplyIn,
  JudgeImportOut,
  JudgeImportPreviewOut,
} from './types'

type ImportQuery = NonNullable<
  operations['judge_import_preview_api_v1_admin_contests__contest_id__judge_import_preview_post']['parameters']['query']
>
type ExportQuery = NonNullable<
  operations['judge_export_api_v1_admin_contests__contest_id__judge_export_get']['parameters']['query']
>

export type JudgeImportFormat = NonNullable<ImportQuery['format']>
export type JudgeExportFormat = NonNullable<ExportQuery['format']>

/** 导入格式选项（auto = 自动识别）。 */
export const JUDGE_IMPORT_FORMATS: { value: JudgeImportFormat; label: string }[] = [
  { value: 'auto', label: '自动识别' },
  { value: 'syncoj', label: 'SyncOJ' },
  { value: 'hoj', label: 'HOJ' },
  { value: 'lemonlime', label: 'LemonLime' },
  { value: 'hydro', label: 'Hydro' },
  { value: 'qduoj', label: 'QDUOJ' },
  { value: 'fps', label: 'FPS' },
]

/** 导出格式选项。 */
export const JUDGE_EXPORT_FORMATS: { value: JudgeExportFormat; label: string }[] = [
  { value: 'syncoj', label: 'SyncOJ' },
  { value: 'hoj', label: 'HOJ' },
  { value: 'lemonlime', label: 'LemonLime' },
  { value: 'hydro', label: 'Hydro' },
]

/** `?format=x&problem_id=1&problem_id=2`：`problem_id` 要重复出现，`query()` 只能 set 一次。 */
function exportQuery(format: JudgeExportFormat, problemIds: number[] = []): string {
  const search = new URLSearchParams({ format })
  for (const id of problemIds) search.append('problem_id', String(id))
  return `?${search.toString()}`
}

export const judgeIoApi = {
  /** 上传题目配置包（zip / FPS xml），返回预览与暂存 token（2 小时有效）。 */
  importPreview: (contestId: number, file: File, format: JudgeImportFormat = 'auto') => {
    const form = new FormData()
    form.append('file', file)
    return request<JudgeImportPreviewOut>(
      `${paths.contestJudgeImportPreview(contestId)}?format=${encodeURIComponent(format)}`,
      { method: 'POST', form },
    )
  },

  /** 应用导入。ident 冲突 409 `ident_conflict`（`details.key` 指出哪一行）；token 过期 404。 */
  importApply: (contestId: number, payload: JudgeImportApplyIn) =>
    request<JudgeImportOut>(paths.contestJudgeImportApply(contestId), { method: 'POST', body: payload }),

  /** 导出前预览：每题是否写进包、会丢掉什么。`problemIds` 为空时按默认范围。 */
  exportPreview: (contestId: number, format: JudgeExportFormat, problemIds: number[] = []) =>
    request<JudgeExportPreviewOut>(
      `${paths.contestJudgeExportPreview(contestId)}${exportQuery(format, problemIds)}`,
    ),

  exportZip: (contestId: number, format: JudgeExportFormat, problemIds: number[] = []) =>
    downloadWithServerName(
      `${paths.contestJudgeExport(contestId)}${exportQuery(format, problemIds)}`,
      `题目配置-${contestId}-${format}.zip`,
    ),

  /** 单题导出。 */
  exportProblem: (problemId: number, format: JudgeExportFormat) =>
    downloadWithServerName(
      `${paths.problemJudgeExport(problemId)}${exportQuery(format)}`,
      `题目配置-${problemId}-${format}.zip`,
    ),

  /** 原样下载数据包。 */
  downloadTestdata: (problemId: number, fallbackName: string) =>
    downloadWithServerName(paths.problemTestdataFile(problemId), fallbackName),
}

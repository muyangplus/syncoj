<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { statusCode, statusTooltip } from '@/utils/judgeStatus'

import { ElMessage, ElMessageBox } from 'element-plus'

import { judgingApi, scoreApi } from '@/api'
import { ApiError } from '@/api/client'
import type { JudgeRunDetailOut, JudgeRunOut, JudgeScanOut, ScoreCellOut, ScoreRowOut } from '@/api/types'
import JudgeRunDetailDialog from '@/components/JudgeRunDetailDialog.vue'
import ScoreTimelineDialog from '@/components/ScoreTimelineDialog.vue'
import ConfirmByNameDialog from '@/components/ConfirmByNameDialog.vue'
import DataTable from '@/components/DataTable.vue'
import JudgeFlagTags from '@/components/JudgeFlagTags.vue'
import FormDialog from '@/components/FormDialog.vue'
import PageShell from '@/components/PageShell.vue'
import SectionCard from '@/components/SectionCard.vue'
import { useList } from '@/composables/useList'
import { useMutation } from '@/composables/useMutation'
import { useContestData } from '@/composables/useContestData'
import { useRoute, useRouter } from 'vue-router'

import { useContestStore } from '@/stores/contest'
import { formatTime } from '@/utils/format'
import { FLAG_FILTERS, flagParam, flagRowClass } from '@/utils/judgeFlags'

const contest = useContestStore()
const router = useRouter()
const route = useRoute()

// --------------------------------------------------------------------------- //
// 成绩矩阵
//
// 矩阵是一个**整体对象**（选手 × 题目一次算全），不是一页数据，所以它走
// `useContestData` 而不是 `useList`；它下面的「评测记录」才是分页集合。
// --------------------------------------------------------------------------- //

const {
  data: matrix,
  loading,
  error,
  reload: reloadMatrix,
} = useContestData((contestId) => scoreApi.matrix(contestId), { interval: 10000 })

/**
 * 归一化后的矩阵。
 *
 * 生成的 TS 类型里 `rows` / `columns` / `cells` 是可选的（pydantic 的
 * `default_factory=list` 在 OpenAPI 里不算 required），但服务端**一定会**发它们。
 * 与其在模板里到处写 `?.` 和 `?? []`，不如在这里补一次默认值 —— 语义更清楚，
 * 模板也更干净；真要遇到字段缺失也不会白屏。
 */
const view = computed(() => {
  const source = matrix.value
  if (!source) return null
  return {
    columns: source.columns ?? [],
    rows: (source.rows ?? []).map((row) => ({ ...row, cells: row.cells ?? [] })),
  }
})

// --------------------------------------------------------------------------- //
// 评测记录
//
// 矩阵只告诉你"这一格是什么"，而"这一格是从哪个文件、用哪个解析器、为什么
// 读不懂"在记录里。以前它藏在一个弹窗里、只看得到当前所有记录；现在它是页面上
// 的一块，并且能按 `parse_status` 过滤 —— 教师排错时第一件事就是"把未解析的
// 都列出来"。
// --------------------------------------------------------------------------- //

/** `all` = 全部（el-select 把空串当成"没选"，只显示占位符）。服务端只认 `parse_status` 这个参数名。 */
const parseFilter = ref('all')
/** 风险标注筛选（§12.11） */
const flagFilter = ref('all')

const runs = useList<JudgeRunOut>({
  rowKey: (row) => row.id,
  loader: ({ limit, offset, signal }) => {
    const contestId = contest.currentId
    // 还没选场次时返回 null —— 列表空着但不报错
    if (!contestId) return Promise.resolve(null)
    return scoreApi.runs(
      contestId,
      { limit, offset, parse_status: parseFilter.value === 'all' ? undefined : parseFilter.value, flag: flagParam(flagFilter.value) },
      signal,
    )
  },
  // 与矩阵同一个节奏：两边看到的是同一批数据
  interval: 10000,
  // 筛选变化要回到第一页 —— `watches` 负责这件事，页面不再自己 watch
  watches: [() => contest.currentId, () => parseFilter.value, () => flagFilter.value],
})

const scanReport = ref<JudgeScanOut | null>(null)
const scanErrors = computed(() => scanReport.value?.errors ?? [])

/** 重扫的回执。`errors` 必须露出来 —— 读不到的目录是"结果没进来"的唯一线索。 */
function scanSummary(report: JudgeScanOut): string {
  const parts = [
    `解析 ${report.parsed}`,
    `未解析 ${report.unparsed}`,
    `未变化 ${report.unchanged}`,
    `跳过 ${report.skipped}`,
    `保留手工录入 ${report.manual}`,
  ]
  // 错误数从传进来的回执里数，而不是读 `scanErrors` —— 这句是在写入 ref **之前**
  // 用来拼 toast 的，读 ref 会拿到上一次的报告
  const errorCount = (report.errors ?? []).length
  if (errorCount) parts.push(`读取错误 ${errorCount}`)
  return parts.join(' · ')
}

const rescan = useMutation(
  async () => {
    const contestId = contest.currentId
    if (!contestId) throw new Error('未选择场次')
    return scoreApi.rescan(contestId)
  },
  {
    success: (report) => `重扫完成：${scanSummary(report)}`,
    onDone: async (report) => {
      scanReport.value = report
      // 矩阵与记录都重拉：重扫刚刚改过它们，等下一轮轮询才更新会让人以为没生效
      await Promise.all([reloadMatrix(), runs.reload()])
    },
  },
)

// --------------------------------------------------------------------------- //
// 清空成绩记录（范围删除）
//
// 它是"重扫之前的清场"：旧记录不抹掉的话，解析器换了、目录结构改了，
// 矩阵里会留着上一批数据。`keepManual` 默认打开 —— 手工录入的分数是全场
// 最贵的数据，不该被"重扫一遍"顺手清掉，服务端的默认值也是它。
// --------------------------------------------------------------------------- //

const clearRunsOpen = ref(false)
const clearKeepManual = ref(true)

const clearRuns = useMutation(
  async () => {
    const contestId = contest.currentId
    if (!contestId) throw new Error('未选择场次')
    return scoreApi.clearRuns(contestId, contest.current?.slug ?? '', {
      keepManual: clearKeepManual.value,
    })
  },
  {
    // 服务端的回执会写清"清除 N 条、保留 M 条手工录入"，直接用，不自己数
    onDone: async () => {
      clearRunsOpen.value = false
      clearKeepManual.value = true
      await Promise.all([reloadMatrix(), runs.reload()])
    },
  },
)

// --------------------------------------------------------------------------- //
// 单元格呈现
// --------------------------------------------------------------------------- //

/**
 * 单元格外观。
 *
 * 核心是**区分"0 分"和"没有成绩"**，以及没有成绩时**交了没评测**与**根本没交**。
 * `kind` 决定分数块的样式，`ratio` 是得分率（0~1，无满分时为 null）。
 */
type CellKind = 'full' | 'high' | 'mid' | 'low' | 'zero' | 'plain' | 'pending' | 'missing' | 'unparsed' | 'noscore'

function cellView(cell: ScoreCellOut | undefined): {
  text: string
  sub: string
  kind: CellKind
  ratio: number | null
  manual: boolean
  tooltip: string
} {
  if (!cell || cell.parse_status === 'missing') {
    if (cell?.submitted) {
      return { text: '待评测', sub: '', kind: 'pending', ratio: null, manual: false, tooltip: '已收到代码，待评测' }
    }
    return { text: '—', sub: '', kind: 'missing', ratio: null, manual: false, tooltip: '未交' }
  }

  const detail = cell.detail ?? ''

  if (cell.parse_status === 'unparsed') {
    return {
      text: '未解析',
      sub: '',
      kind: 'unparsed',
      ratio: null,
      manual: false,
      tooltip: `未解析：${detail || '无原因'}`,
    }
  }

  if (cell.score === null || cell.score === undefined) {
    return { text: '无分', sub: '', kind: 'noscore', ratio: null, manual: false, tooltip: detail || '无分数' }
  }

  const hasMax = cell.max_score !== null && cell.max_score !== undefined && cell.max_score > 0
  const ratio = hasMax ? Math.max(0, Math.min(1, cell.score / (cell.max_score as number))) : null
  let kind: CellKind
  if (cell.score === 0) kind = 'zero'
  else if (ratio === null) kind = 'plain'
  else if (ratio >= 1) kind = 'full'
  else if (ratio >= 0.6) kind = 'high'
  else if (ratio >= 0.3) kind = 'mid'
  else kind = 'low'

  const parts = [`${cell.score}${hasMax ? ` / ${cell.max_score}` : ''} 分`]
  if (cell.status) parts.push(statusTooltip(cell.status))
  if (cell.parse_status === 'manual') parts.push('手工录入')
  if (cell.risk_count) parts.push(`风险 ${cell.risk_count}`)
  if (cell.warning_count) parts.push(`疑似违规 ${cell.warning_count}`)
  if (detail) parts.push(detail)

  return {
    text: String(cell.score),
    sub: (cell.status && statusCode(cell.status)) || (hasMax ? `/ ${cell.max_score}` : ''),
    kind,
    ratio,
    manual: cell.parse_status === 'manual',
    tooltip: parts.join(' · '),
  }
}

/** 格子的风险细边框（不改分数颜色）：风险红、疑似违规黄 */
function cellFlagClass(cell: ScoreCellOut | undefined): string {
  if (cell?.risk_count) return 'flag-risk'
  if (cell?.warning_count) return 'flag-warning'
  return ''
}

function cellOf(row: ScoreRowOut, ident: string): ScoreCellOut | undefined {
  return row.cells?.find((cell) => cell.problem === ident)
}

// --------------------------------------------------------------------------- //
// 手工录分（矩阵与记录共用同一个弹窗）
// --------------------------------------------------------------------------- //

/**
 * 录分入口的上下文。
 *
 * 矩阵格子与评测记录行都能开这个弹窗 —— 后者的意义是"看到一条未解析记录，
 * 就地补录"，不用先回矩阵里找那一格。字段全给可选，两个来源的行对象就都能塞进来。
 */
interface ManualSeed {
  score?: number | null
  max_score?: number | null
  status?: string | null
  parse_status?: string
  detail?: string | null
}

const editing = ref(false)
const manualSeed = ref<ManualSeed | null>(null)
const form = reactive({
  playerId: 0,
  playerNo: '',
  problem: '',
  score: 0,
  maxScore: 100,
  status: '',
})

/** 从"未解析"的格子/记录进来时，把解析器给出的原因摆在录入框上面。 */
const manualUnparsed = computed(() => manualSeed.value?.parse_status === 'unparsed')

function openManual(
  target: { player_id: number; player_no: string },
  problem: string,
  seed?: ManualSeed,
): void {
  form.playerId = target.player_id
  form.playerNo = target.player_no
  form.problem = problem
  form.score = seed?.score ?? 0
  form.maxScore = seed?.max_score ?? 100
  form.status = seed?.status ?? ''
  manualSeed.value = seed ?? null
  editing.value = true
}

const saveManual = useMutation(
  async () => {
    const contestId = contest.currentId
    if (!contestId) throw new Error('未选择场次')
    return scoreApi.setManual(contestId, {
      player_id: form.playerId,
      problem: form.problem,
      score: form.score,
      max_score: form.maxScore,
      status: form.status.trim() || null,
    })
  },
  {
    // `JudgeRunOut` 上没有服务端的回执，自己拼一句，并把"不会被覆盖"这个关键约定写出来
    success: '已保存',
    onDone: async () => {
      editing.value = false
      await Promise.all([reloadMatrix(), runs.reload()])
    },
  },
)

/**
 * 清除某一格是**硬删除**一条评测记录，按约定要"把名字打一遍"而不是"是否确定" ——
 * 连续清格子时，同一位置的确认按钮会被手指肌肉记忆点掉。
 * 服务端要的 `confirm` 正好是考号，矩阵行首显示的也是考号，教师照着打就行。
 */
const clearCellOpen = ref(false)

function askClear(): void {
  clearCellOpen.value = true
}

const clearScore = useMutation(
  async () => {
    const contestId = contest.currentId
    if (!contestId) throw new Error('未选择场次')
    // 第 4 个参数是考号：服务端拿它逐字比对 `confirm`
    return scoreApi.clear(contestId, form.playerId, form.problem, form.playerNo)
  },
  {
    success: '已清除',
    onDone: async () => {
      clearCellOpen.value = false
      editing.value = false
      await Promise.all([reloadMatrix(), runs.reload()])
    },
  },
)

// --------------------------------------------------------------------------- //
// 导出 CSV
// --------------------------------------------------------------------------- //

async function exportCsv(): Promise<void> {
  const data = view.value
  if (!data) return
  // 列名用「标题（标识）」或纯标识 —— 教师看标题，机器认标识
  const header = [
    '选手编号',
    '姓名',
    '总分',
    ...data.columns.map((c) => (c.title ? `${c.title}(${c.ident})` : c.ident)),
  ]
  const lines = [header.join(',')]
  for (const row of data.rows) {
    const cells = data.columns.map((column) => {
      const cell = row.cells.find((c) => c.problem === column.ident)
      if (!cell || cell.parse_status === 'missing') {
        // 导出里也要分清：空着是"没交"，"待评测"是交了的
        return cell?.submitted ? '待评测' : ''
      }
      if (cell.parse_status === 'unparsed') return '未解析'
      return cell.score ?? ''
    })
    lines.push([row.player_no, row.player_name ?? '', row.total ?? 0, ...cells].join(','))
  }
  // 加 BOM，否则 Excel 打开中文会乱码
  const blob = new Blob(['\ufeff' + lines.join('\n')], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `scores-${contest.currentId ?? 'contest'}.csv`
  anchor.click()
  URL.revokeObjectURL(url)
}

/** 快捷操作 `?do=export`：矩阵就绪后导出一次，然后清掉 query。 */
watch(
  () => [route.query.do, view.value, error.value] as const,
  ([action, data, failed]) => {
    if (action !== 'export') return
    if (!data && !failed && contest.currentId) return
    if (data?.rows.length) void exportCsv()
    else if (!failed) ElMessage.info('无成绩数据')
    void router.replace({ query: { ...route.query, do: undefined } })
  },
  { immediate: true },
)

function runStatusType(parseStatus: string): 'success' | 'danger' | 'primary' | 'info' {
  if (parseStatus === 'ok') return 'success'
  if (parseStatus === 'unparsed') return 'danger'
  if (parseStatus === 'manual') return 'primary'
  return 'info'
}

function runStatusLabel(parseStatus: string): string {
  const map: Record<string, string> = {
    ok: '已解析',
    unparsed: '未解析',
    manual: '手工录入',
    missing: '无记录',
  }
  return map[parseStatus] ?? parseStatus
}

// --------------------------------------------------------------------------- //
// 测试点明细（§12）
// --------------------------------------------------------------------------- //

const detailOpen = ref(false)
const detailRunId = ref<number | null>(null)

function openDetail(runId: number): void {
  detailRunId.value = runId
  detailOpen.value = true
}

/** 带测试点明细的格子打开明细，其余打开手工录分。 */
function openCell(row: ScoreRowOut, ident: string): void {
  const cell = cellOf(row, ident)
  if (cell?.has_cases && cell.run_id) {
    openDetail(cell.run_id)
    return
  }
  openManual(row, ident, cell)
}

function manualFromDetail(run: JudgeRunDetailOut): void {
  detailOpen.value = false
  openManual(run, run.problem, run)
}

// --------------------------------------------------------------------------- //
// 版本与分数 timeline（§12.9）
// --------------------------------------------------------------------------- //

const timelineOpen = ref(false)
const timelineTarget = ref<{ playerId: number; playerNo: string; problem: string | null } | null>(null)

function openTimeline(playerId: number, playerNo: string, problem: string | null): void {
  timelineTarget.value = { playerId, playerNo, problem }
  timelineOpen.value = true
}

function timelineFromDetail(run: JudgeRunDetailOut): void {
  detailOpen.value = false
  openTimeline(run.player_id, run.player_no, run.problem)
}

async function rejudge(run: JudgeRunDetailOut, force = false): Promise<void> {
  const contestId = contest.currentId
  if (!contestId) return
  try {
    const result = await judgingApi.queue(contestId, { problem: run.problem, player_id: run.player_id, force, rerun: false })
    ElMessage.success(result.queued ? '已排队重测' : '未排队：该题不是自带评测')
  } catch (err) {
    if (err instanceof ApiError && err.code === 'contest_in_progress' && !force) {
      try {
        await ElMessageBox.confirm('场次未结束，将评测当前已回收的代码。', '仍要重测？', {
          type: 'warning',
          confirmButtonText: '仍要重测',
          cancelButtonText: '取消',
        })
      } catch {
        return
      }
      await rejudge(run, true)
      return
    }
    ElMessage.error((err as Error).message)
  }
}
</script>

<template>
  <PageShell title="成绩" :error="error" retryable @retry="reloadMatrix">
    <template #toolbar>
      <el-button type="primary" :loading="rescan.pending.value" @click="rescan.run(undefined)">
        <el-icon><Refresh /></el-icon>立即重扫
      </el-button>
      <el-button :disabled="!view?.rows.length" @click="exportCsv">
        <el-icon><Download /></el-icon>导出 CSV
      </el-button>
      <el-button @click="router.push({ name: 'judge' })">
        <el-icon><Cpu /></el-icon>自带评测
      </el-button>
      <el-dropdown trigger="click">
        <el-button>
          更多<el-icon class="el-icon--right"><ArrowDown /></el-icon>
        </el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item :disabled="loading" @click="reloadMatrix">
              <el-icon><RefreshRight /></el-icon>刷新
            </el-dropdown-item>
            <el-dropdown-item divided @click="clearRunsOpen = true">
              <span class="danger-item"><el-icon><Delete /></el-icon>清空成绩记录</span>
            </el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </template>

    <!-- 重扫回执常驻：errors 是"交了却没分数"的排查起点 -->
    <el-alert
      v-if="scanReport"
      :type="scanErrors.length ? 'error' : scanReport.unparsed ? 'warning' : 'success'"
      :closable="true"
      show-icon
      class="scan-alert"
      @close="scanReport = null"
    >
      <template #title>上次重扫：{{ scanSummary(scanReport) }}</template>
      <template #default>
        <ul v-if="scanErrors.length" class="scan-errors">
          <li v-for="(item, index) in scanErrors" :key="index">{{ item }}</li>
        </ul>
      </template>
    </el-alert>

    <SectionCard
      title="成绩矩阵"
      :meta="view && view.columns.length ? `${view.rows.length} 名选手 · ${view.columns.length} 道题` : ''"
      flush
    >
      <template v-if="view && view.columns.length" #extra>
        <div class="legend">
          <span class="legend-item"><i class="swatch full" />满分</span>
          <span class="legend-item"><i class="swatch mid" />部分分</span>
          <span class="legend-item"><i class="swatch zero" />0 分</span>
          <span class="legend-item"><i class="swatch pending" />待评测</span>
          <span class="legend-item"><i class="swatch unparsed" />未解析</span>
          <span class="legend-item"><i class="swatch missing" />未交</span>
        </div>
      </template>

      <el-table v-if="view && view.columns.length" :data="view.rows" v-loading="loading" class="matrix">
        <el-table-column label="选手" width="150" fixed>
          <template #default="{ row }">
            <div class="player-cell">
              <div>
                <div class="mono">{{ row.player_no }}</div>
                <div class="cell-sub">{{ row.player_name || '—' }}</div>
              </div>
              <el-tooltip content="版本与分数" :show-after="300">
                <el-button
                  link
                  type="primary"
                  size="small"
                  aria-label="版本与分数"
                  @click="openTimeline(row.player_id, row.player_no, null)"
                >
                  <el-icon><TrendCharts /></el-icon>
                </el-button>
              </el-tooltip>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="总分" width="84" align="right" fixed>
          <template #default="{ row }">
            <span class="total">{{ row.total ?? 0 }}</span>
          </template>
        </el-table-column>

        <el-table-column v-for="column in view.columns" :key="column.ident" min-width="108" align="center">
          <template #header>
            <div>{{ column.title || column.ident }}</div>
            <div v-if="column.title" class="cell-sub mono">{{ column.ident }}</div>
            <el-tag v-else-if="!column.declared" size="small" type="warning" effect="plain">未登记</el-tag>
          </template>
          <template #default="{ row }">
            <el-tooltip :content="cellView(cellOf(row, column.ident)).tooltip" :show-after="200">
              <button
                type="button"
                class="score-block"
                :class="[`is-${cellView(cellOf(row, column.ident)).kind}`, cellFlagClass(cellOf(row, column.ident))]"
                @click="openCell(row, column.ident)"
              >
                <span class="score-main">{{ cellView(cellOf(row, column.ident)).text }}</span>
                <span v-if="cellView(cellOf(row, column.ident)).sub" class="score-sub">
                  {{ cellView(cellOf(row, column.ident)).sub }}
                </span>
                <el-icon v-if="cellView(cellOf(row, column.ident)).manual" class="score-manual">
                  <EditPen />
                </el-icon>
                <span
                  v-if="cellView(cellOf(row, column.ident)).ratio !== null"
                  class="score-bar"
                  :style="{ width: `${(cellView(cellOf(row, column.ident)).ratio ?? 0) * 100}%` }"
                />
              </button>
            </el-tooltip>
          </template>
        </el-table-column>

        <template #empty>
          <div class="empty-block">本场次无选手</div>
        </template>
      </el-table>

      <div v-else v-loading="loading" class="empty-block">
        <p>无成绩数据</p>
        <el-button type="primary" plain @click="router.push({ name: 'contests' })">登记题目</el-button>
      </div>
    </SectionCard>

    <div class="runs-anchor">
      <SectionCard title="评测记录" :meta="`共 ${runs.total.value} 条`" flush>
        <template #extra>
          <el-select v-model="flagFilter" style="width: 130px" aria-label="风险标注">
            <el-option v-for="f in FLAG_FILTERS" :key="f.value" :label="f.label" :value="f.value" />
          </el-select>
          <el-select v-model="parseFilter" style="width: 140px">
            <el-option label="全部记录" value="all" />
            <el-option label="未解析" value="unparsed" />
            <el-option label="已解析" value="ok" />
            <el-option label="手工录入" value="manual" />
          </el-select>
          <el-button :loading="runs.loading.value" @click="runs.reload">
            <el-icon><RefreshRight /></el-icon>刷新
          </el-button>
        </template>

        <DataTable
          :rows="runs.rows.value"
          :row-key="(row: JudgeRunOut) => row.id"
          :loading="runs.loading.value"
          :total="runs.total.value"
          :page="runs.page.value"
          :page-size="runs.pageSize.value"
          :filtered="parseFilter !== 'all' || flagFilter !== 'all'"
          :row-class="flagRowClass"
          empty-text="无评测记录"
          empty-filtered-text="无匹配的评测记录"
          @update:page="runs.setPage"
          @update:pageSize="runs.setPageSize"
        >
          <template #columns>
            <el-table-column label="选手" prop="player_no" width="110" />
            <el-table-column label="题目" prop="problem" width="100" />
            <el-table-column label="状态" width="100">
              <template #default="{ row }">
                <el-tag :type="runStatusType(row.parse_status)" size="small" effect="light">
                  {{ runStatusLabel(row.parse_status) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="得分" width="130" align="right">
              <template #default="{ row }">
                <span v-if="row.score === null || row.score === undefined" class="muted">—</span>
                <span v-else>{{ row.score }} / {{ row.max_score ?? '?' }}</span>
                <div v-if="row.risk_count || row.warning_count" class="run-flags">
                  <JudgeFlagTags :warning="row.warning_count" :risk="row.risk_count" />
                </div>
              </template>
            </el-table-column>
            <el-table-column label="来源文件" min-width="220">
              <template #default="{ row }">
                <span v-if="row.source_path" class="mono">{{ row.source_path }}</span>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column label="信息" min-width="200">
              <template #default="{ row }">
                <span :class="{ 'error-text': row.parse_status === 'unparsed' }" class="run-detail">
                  {{ row.detail || '—' }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="更新时间" width="170">
              <template #default="{ row }">
                <span class="cell-sub">{{ formatTime(row.updated_at) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="110" fixed="right">
              <template #default="{ row }">
                <el-button v-if="row.has_cases" link type="primary" size="small" @click="openDetail(row.id)">
                  明细
                </el-button>
                <el-button link type="primary" size="small" @click="openManual(row, row.problem, row)">
                  {{ row.parse_status === 'unparsed' ? '补录' : '修正' }}
                </el-button>
              </template>
            </el-table-column>
          </template>
        </DataTable>
      </SectionCard>
    </div>

    <FormDialog
      v-model="editing"
      title="录入成绩"
      width="480px"
      :submitting="saveManual.pending.value"
      @submit="saveManual.run(undefined)"
    >
      <el-alert v-if="manualUnparsed" type="warning" :closable="false" show-icon class="manual-alert">
        <template #title>未解析：{{ manualSeed?.detail || '无原因' }}</template>
      </el-alert>
      <el-form label-width="72px">
        <el-form-item label="选手">
          <span class="mono">{{ form.playerNo }}</span>
        </el-form-item>
        <el-form-item label="题目">
          <span class="mono">{{ form.problem }}</span>
        </el-form-item>
        <el-form-item label="得分">
          <el-input-number v-model="form.score" :min="0" :max="100000" />
        </el-form-item>
        <el-form-item label="满分">
          <el-input-number v-model="form.maxScore" :min="0" :max="100000" />
        </el-form-item>
        <el-form-item label="状态">
          <el-input v-model="form.status" placeholder="例如 AC / WA" />
        </el-form-item>
      </el-form>

      <template #footer-prepend>
        <el-button
          v-if="form.playerId"
          link
          type="danger"
          :loading="clearScore.pending.value"
          @click="askClear"
        >
          清除成绩
        </el-button>
      </template>
    </FormDialog>

    <!-- 清除一格 = 硬删除一条评测记录；confirm 是考号 -->
    <ConfirmByNameDialog
      v-model="clearCellOpen"
      title="清除成绩？"
      :expected="form.playerNo"
      :submitting="clearScore.pending.value"
      confirm-text="清除"
      :detail="`将删除 ${form.playerNo}「${form.problem}」的成绩记录（含手工录入），不可恢复；代码与结果文件不受影响。`"
      @confirm="clearScore.run(undefined)"
    />

    <!-- 清空 = 整个场次的范围删除，不跟随记录区筛选；confirm 是场次标识 -->
    <ConfirmByNameDialog
      v-model="clearRunsOpen"
      title="清空成绩记录？"
      :expected="contest.current?.slug ?? ''"
      :submitting="clearRuns.pending.value"
      confirm-text="清空"
      detail="将删除本场次全部评测记录（不受筛选影响），不可恢复。"
      @confirm="clearRuns.run(undefined)"
    >
      <el-checkbox v-model="clearKeepManual">保留手工录入的分数</el-checkbox>
      <el-alert
        v-if="!clearKeepManual"
        type="error"
        :closable="false"
        show-icon
        class="manual-alert"
        title="手工录入的分数将一并删除，重扫无法恢复。"
      />
    </ConfirmByNameDialog>

    <JudgeRunDetailDialog
      v-model="detailOpen"
      :contest-id="contest.currentId"
      :run-id="detailRunId"
      @manual="manualFromDetail"
      @rejudge="rejudge"
      @timeline="timelineFromDetail"
    />

    <ScoreTimelineDialog
      v-model="timelineOpen"
      :contest-id="contest.currentId"
      :player-id="timelineTarget?.playerId ?? null"
      :player-no="timelineTarget?.playerNo ?? ''"
      :problem="timelineTarget?.problem ?? null"
      @changed="reloadMatrix"
    />
  </PageShell>
</template>

<style scoped>
.player-cell {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}
.clickable {
  cursor: pointer;
}

.scan-alert {
  margin-bottom: 16px;
}

.scan-errors {
  margin: 4px 0;
  padding-left: 18px;
  max-height: 200px;
  overflow: auto;
}

.danger-item {
  display: inline-flex;
  align-items: center;
  color: var(--sj-danger);
}

/* ---- 图例 ---- */

.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 12px;
  color: var(--sj-text-3);
}

.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.swatch {
  display: inline-block;
  width: 12px;
  height: 12px;
  border-radius: 3px;
  border: 1px solid transparent;
}

.swatch.full {
  background: var(--sj-success-soft);
  border-color: var(--sj-success);
}

.swatch.mid {
  background: var(--sj-warning-soft);
  border-color: var(--sj-warning);
}

.swatch.zero {
  background: var(--sj-danger-soft);
  border-color: var(--sj-danger);
}

.swatch.pending {
  border: 1px dashed var(--sj-primary);
}

.swatch.unparsed {
  border: 1px dashed var(--sj-danger);
}

.swatch.missing {
  border: 1px dashed var(--sj-border-strong);
}

/* ---- 矩阵 ---- */

.matrix .total {
  font-size: 15px;
  font-weight: 700;
  color: var(--sj-text);
}

.score-block {
  --block-fg: var(--sj-text-2);
  --block-bg: var(--sj-info-soft);
  --block-border: transparent;
  position: relative;
  display: inline-flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  width: 100%;
  max-width: 92px;
  min-height: 40px;
  padding: 4px 6px 6px;
  overflow: hidden;
  border: 1px solid var(--block-border);
  border-radius: var(--sj-radius-sm);
  background: var(--block-bg);
  color: var(--block-fg);
  font: inherit;
  line-height: 1.2;
  cursor: pointer;
  transition:
    transform 0.12s ease,
    box-shadow 0.12s ease;
}

.score-block:hover {
  transform: translateY(-1px);
  box-shadow: var(--sj-shadow-lg);
}

.score-block:focus-visible {
  outline: 2px solid var(--sj-primary);
  outline-offset: 1px;
}

.score-main {
  font-size: 15px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.score-sub {
  margin-top: 1px;
  font-size: 11px;
  font-weight: 500;
  opacity: 0.8;
}

.score-manual {
  position: absolute;
  top: 3px;
  right: 3px;
  font-size: 11px;
  opacity: 0.75;
}

.score-bar {
  position: absolute;
  left: 0;
  bottom: 0;
  height: 3px;
  background: currentColor;
  opacity: 0.55;
}

.score-block.is-full {
  --block-fg: var(--sj-success);
  --block-bg: var(--sj-success-soft);
  --block-border: var(--sj-success);
}

.score-block.is-high {
  --block-fg: var(--sj-primary);
  --block-bg: var(--sj-primary-soft);
}

.score-block.is-mid {
  --block-fg: var(--sj-warning);
  --block-bg: var(--sj-warning-soft);
}

.score-block.is-low {
  --block-fg: var(--sj-danger);
  --block-bg: var(--sj-warning-soft);
}

.score-block.is-zero {
  --block-fg: var(--sj-danger);
  --block-bg: var(--sj-danger-soft);
  --block-border: var(--sj-danger);
}

.score-block.is-plain {
  --block-fg: var(--sj-text);
}

.score-block.is-pending,
.score-block.is-unparsed,
.score-block.is-noscore,
.score-block.is-missing {
  --block-bg: transparent;
  border-style: dashed;
}

.score-block.is-pending .score-main,
.score-block.is-unparsed .score-main,
.score-block.is-noscore .score-main {
  font-size: 12.5px;
  font-weight: 600;
}

.score-block.is-pending {
  --block-fg: var(--sj-primary);
  --block-border: var(--sj-primary);
  --block-bg: var(--sj-primary-soft);
}

.score-block.is-unparsed {
  --block-fg: var(--sj-danger);
  --block-border: var(--sj-danger);
}

.score-block.is-noscore {
  --block-fg: var(--sj-warning);
  --block-border: var(--sj-warning);
}

.score-block.is-missing {
  --block-fg: var(--sj-text-3);
  --block-border: var(--sj-border-strong);
}

/* 风险标注（§12.11）：细边框，不改分数颜色 */
.score-block.flag-warning {
  box-shadow: 0 0 0 2px var(--sj-warning-line);
}

.score-block.flag-risk {
  box-shadow: 0 0 0 2px var(--sj-danger-line);
}

.score-block.flag-warning:hover {
  box-shadow: 0 0 0 2px var(--sj-warning-line), var(--sj-shadow-lg);
}

.score-block.flag-risk:hover {
  box-shadow: 0 0 0 2px var(--sj-danger-line), var(--sj-shadow-lg);
}

/* ---- 评测记录 ---- */

.run-flags {
  margin-top: 4px;
}

.runs-anchor {
  margin-top: 16px;
  scroll-margin-top: calc(var(--sj-topbar-h) + 16px);
}

.run-detail {
  font-size: 12.5px;
}

.manual-alert {
  margin-bottom: 14px;
}

.el-checkbox + .manual-alert {
  margin: 8px 0 0;
}
</style>


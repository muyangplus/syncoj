<script setup lang="ts">
import { computed, nextTick, reactive, ref } from 'vue'

import { scoreApi } from '@/api'
import type { JudgeRunOut, JudgeScanOut, ScoreCellOut, ScoreRowOut } from '@/api/types'
import ConfirmByNameDialog from '@/components/ConfirmByNameDialog.vue'
import DataTable from '@/components/DataTable.vue'
import FormDialog from '@/components/FormDialog.vue'
import PageShell from '@/components/PageShell.vue'
import { useList } from '@/composables/useList'
import { useMutation } from '@/composables/useMutation'
import { useContestData } from '@/composables/useContestData'
import { useRouter } from 'vue-router'

import { useContestStore } from '@/stores/contest'
import { formatTime } from '@/utils/format'

const contest = useContestStore()
const router = useRouter()

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

/** 空串 = 全部。服务端只认 `parse_status` 这个参数名。 */
const parseFilter = ref('')

const runs = useList<JudgeRunOut>({
  rowKey: (row) => row.id,
  loader: ({ limit, offset, signal }) => {
    const contestId = contest.currentId
    // 还没选场次时返回 null —— 列表空着但不报错
    if (!contestId) return Promise.resolve(null)
    return scoreApi.runs(
      contestId,
      { limit, offset, parse_status: parseFilter.value || undefined },
      signal,
    )
  },
  // 与矩阵同一个节奏：两边看到的是同一批数据
  interval: 10000,
  // 筛选变化要回到第一页 —— `watches` 负责这件事，页面不再自己 watch
  watches: [() => contest.currentId, () => parseFilter.value],
})

const runsSection = ref<HTMLElement | null>(null)
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
    if (!contestId) throw new Error('还没有选场次')
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

/** 把"未解析"作为筛选条件带到记录区，并滚过去 —— 不滚的话长页面上等于点了没反应。 */
function showUnparsedRuns(): void {
  parseFilter.value = 'unparsed'
  void nextTick(() => runsSection.value?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
}

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
    if (!contestId) throw new Error('还没有选场次')
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
 * 这里的核心是**区分"0 分"和"没有成绩"** —— 后端刻意把两者分成不同的
 * parse_status，前端也必须分开呈现。都显示成 0 会让教师以为选手考砸了。
 *
 * 再往下一层：没有成绩时还要分清**交了没评测**和**根本没交**。两件事看起来
 * 都是"没有分"，但教师该做的事完全相反 —— 一个等着就行，一个得去座位上看。
 */
function cellView(cell: ScoreCellOut | undefined): {
  text: string
  type: 'success' | 'danger' | 'warning' | 'info' | 'primary'
  tooltip: string
} {
  if (!cell || cell.parse_status === 'missing') {
    if (cell?.submitted) {
      return {
        text: '待评测',
        type: 'primary',
        tooltip:
          '已收到这个题目的代码，还没等到评测结果。\n可以到下面「评测记录」里看，或直接点这一格手工录分。',
      }
    }
    return { text: '—', type: 'info', tooltip: '没有收到这个题目的代码' }
  }

  const detail = cell.detail ?? ''

  if (cell.parse_status === 'unparsed') {
    return {
      text: '未解析',
      type: 'danger',
      tooltip:
        `收到了结果文件但无法解析，需要手工补录 —— 点这一格就能录，` +
        `也可以到下面「评测记录」里按「补录」。\n原因：${detail || '（服务端没有给出原因）'}`,
    }
  }

  if (cell.score === null || cell.score === undefined) {
    return { text: '无分', type: 'warning', tooltip: detail || '结果中没有分数' }
  }

  const full = cell.max_score !== null && cell.max_score !== undefined
  const isFull = full && cell.score === cell.max_score
  const isZero = cell.score === 0

  const parts = [`${cell.score}${full ? ` / ${cell.max_score}` : ''} 分`]
  if (cell.status) parts.push(`状态：${cell.status}`)
  if (cell.parse_status === 'manual') parts.push('（教师手工录入）')
  else if (cell.submitted) parts.push('（已收到该题代码）')
  if (detail) parts.push(detail)

  return {
    text: full ? `${cell.score}/${cell.max_score}` : String(cell.score),
    type: cell.parse_status === 'manual' ? 'primary' : isFull ? 'success' : isZero ? 'info' : 'warning',
    tooltip: parts.join('\n'),
  }
}

function cellOf(row: ScoreRowOut, ident: string): ScoreCellOut | undefined {
  return row.cells?.find((cell) => cell.problem === ident)
}

interface CellRef {
  row: ScoreRowOut
  cell: ScoreCellOut
}

/** 按题目筛出符合条件的所有格子。 */
function pickCells(predicate: (cell: ScoreCellOut) => boolean): CellRef[] {
  const result: CellRef[] = []
  for (const row of view.value?.rows ?? []) {
    for (const cell of row.cells) {
      if (predicate(cell)) result.push({ row, cell })
    }
  }
  return result
}

const unparsedCells = computed(() => pickCells((cell) => cell.parse_status === 'unparsed'))

/**
 * 「交了，但评测还没出结果」。
 *
 * 这一栏是给教师吃定心丸用的：机器在跑、文件收到了，等着就行 ——
 * 以前这种格子显示成「—」，和"根本没交"长得一模一样，只能逐个点开看。
 */
const pendingCells = computed(() =>
  pickCells((cell) => cell.parse_status === 'missing' && cell.submitted === true),
)

/**
 * 「一题都没交」。真正需要人去催的那一栏。
 *
 * 只统计**清单里登记过的题目** —— 未登记的题目可能只是历史遗留，
 * 拿它去质问选手是错的。
 */
const missingCells = computed(() => {
  const declared = new Set(
    (view.value?.columns ?? []).filter((column) => column.declared).map((c) => c.ident),
  )
  return pickCells(
    (cell) =>
      cell.parse_status === 'missing' &&
      cell.submitted !== true &&
      declared.has(cell.problem),
  )
})

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
    if (!contestId) throw new Error('还没有选场次')
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
    success: '已保存。',
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
    if (!contestId) throw new Error('还没有选场次')
    // 第 4 个参数是考号：服务端拿它逐字比对 `confirm`
    return scoreApi.clear(contestId, form.playerId, form.problem, form.playerNo)
  },
  {
    success: '已清除。结果文件还在的话，下一轮扫描会重新解析出这一格。',
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
</script>

<template>
  <PageShell
    title="成绩"
    :error="error"
    error-action="先重试；还不行就先别手工录分。"
    retryable
    @retry="reloadMatrix"
  >
    <template #toolbar>
      <el-button size="small" :loading="rescan.pending.value" @click="rescan.run(undefined)">
        立即重扫
      </el-button>
      <el-button size="small" :disabled="!view?.rows.length" @click="exportCsv">导出 CSV</el-button>
      <el-button size="small" :loading="loading" @click="reloadMatrix">刷新</el-button>
    </template>

    <!--
      重扫的回执必须留在页面上，不能只弹一句 toast：`errors` 里是"哪个目录读不了、
      哪个目录名对不上人"，它一闪而过的话教师根本来不及看 —— 而那正是
      "明明交了却没分数"的排查起点。
    -->
    <el-alert
      v-if="scanReport"
      :type="scanErrors.length ? 'error' : scanReport.unparsed ? 'warning' : 'success'"
      :closable="true"
      show-icon
      style="margin-bottom: 12px"
      @close="scanReport = null"
    >
      <template #title>上次重扫：{{ scanSummary(scanReport) }}</template>
      <template #default>
        <ul v-if="scanErrors.length" class="scan-errors">
          <li v-for="(item, index) in scanErrors" :key="index">{{ item }}</li>
        </ul>
      </template>
    </el-alert>

    <!-- 未解析项单独提示：这是"需要教师动手"的待办，不该混在矩阵里被忽略 -->
    <el-alert
      v-if="unparsedCells.length"
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom: 12px"
    >
      <template #title>
        有 {{ unparsedCells.length }} 格收到了结果文件但解析不出来
      </template>
      <template #default>
        <div class="unparsed-list">
          <el-tag
            v-for="item in unparsedCells.slice(0, 20)"
            :key="`${item.row.player_id}-${item.cell.problem}`"
            size="small"
            type="danger"
            effect="plain"
            class="clickable"
            @click="openManual(item.row, item.cell.problem, item.cell)"
          >
            {{ item.row.player_no }} / {{ item.cell.problem }}
          </el-tag>
          <span v-if="unparsedCells.length > 20" class="muted">
            等 {{ unparsedCells.length }} 项，点击标签可手工补录
          </span>
          <el-button link type="primary" size="small" @click="showUnparsedRuns">
            在评测记录里逐条看
          </el-button>
        </div>
      </template>
    </el-alert>

    <!-- 交了但没出成绩：告诉教师"在跑，别急"，而不是让人以为漏收了 -->
    <el-alert
      v-if="pendingCells.length"
      type="info"
      :closable="false"
      show-icon
      style="margin-bottom: 12px"
    >
      <template #title>
        有 {{ pendingCells.length }} 格已收到代码，正等评测结果
      </template>
      <template #default>
        <div class="unparsed-list">
          <el-tag
            v-for="item in pendingCells.slice(0, 20)"
            :key="`${item.row.player_id}-${item.cell.problem}`"
            size="small"
            type="primary"
            effect="plain"
          >
            {{ item.row.player_no }} / {{ item.cell.problem }}
          </el-tag>
          <span v-if="pendingCells.length > 20" class="muted">
            也可以直接点格子手工录分。
          </span>
        </div>
      </template>
    </el-alert>

    <!-- 一题都没交：这才是需要人去催的名单 -->
    <el-alert
      v-if="missingCells.length"
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom: 12px"
    >
      <template #title>
        有 {{ missingCells.length }} 格还没收到代码
      </template>
      <template #default>
        <div class="unparsed-list">
          <el-tag
            v-for="item in missingCells.slice(0, 20)"
            :key="`${item.row.player_id}-${item.cell.problem}`"
            size="small"
            type="warning"
            effect="plain"
          >
            {{ item.row.player_no }} / {{ item.cell.problem }}
          </el-tag>
          <span v-if="missingCells.length > 20" class="muted">
            可在「题目清单 → 路径试算」确认。
          </span>
        </div>
      </template>
    </el-alert>

    <el-table
      v-if="view && view.columns.length"
      :data="view.rows"
      v-loading="loading"
      border
      stripe
      size="small"
    >
      <el-table-column label="选手" width="160" fixed>
        <template #default="{ row }">
          <div class="mono">{{ row.player_no }}</div>
          <div class="cell-sub">{{ row.player_name || '—' }}</div>
        </template>
      </el-table-column>

      <el-table-column label="总分" width="90" align="right" fixed>
        <template #default="{ row }">
          <strong>{{ row.total ?? 0 }}</strong>
        </template>
      </el-table-column>

      <el-table-column
        v-for="column in view.columns"
        :key="column.ident"
        width="130"
        align="center"
      >
        <!-- 列头优先显示标题，副行显示标识（标识才是目录名与文件名） -->
        <template #header>
          <div>{{ column.title || column.ident }}</div>
          <div v-if="column.title" class="cell-sub mono">{{ column.ident }}</div>
          <!-- 结果里有、清单里没有的题目：标注出来提醒去补登记 -->
          <el-tag
            v-else-if="!column.declared"
            size="small"
            type="warning"
            effect="plain"
          >
            未登记
          </el-tag>
        </template>
        <template #default="{ row }">
          <el-tooltip :content="cellView(cellOf(row, column.ident)).tooltip">
            <el-tag
              :type="cellView(cellOf(row, column.ident)).type"
              size="small"
              effect="plain"
              class="clickable"
              @click="openManual(row, column.ident, cellOf(row, column.ident))"
            >
              {{ cellView(cellOf(row, column.ident)).text }}
            </el-tag>
          </el-tooltip>
        </template>
      </el-table-column>

      <template #empty>
        <div class="empty-block">还没有选手</div>
      </template>
    </el-table>

    <el-card v-else shadow="never">
      <div class="empty-block">
        <p>先登记题目清单，或点右上角「立即重扫」。</p>
        <el-button
          type="primary"
          size="small"
          style="margin-top: 12px"
          @click="router.push({ name: 'contests' })"
        >
          去场次管理登记题目
        </el-button>
      </div>
    </el-card>

    <!--
      评测记录：矩阵里每一格的来源。它必须能按 `parse_status` 过滤，
      因为教师排错的第一步永远是"把未解析的都列出来"。
    -->
    <div ref="runsSection" style="margin-top: 24px">
      <div class="list-head">
        <h3 class="section-title">
          评测记录
          <span class="muted">（共 {{ runs.total.value }} 条）</span>
        </h3>
        <div class="toolbar">
          <el-select v-model="parseFilter" size="small" style="width: 180px">
            <el-option label="全部记录" value="" />
            <el-option label="未解析（需要处理）" value="unparsed" />
            <el-option label="已解析" value="ok" />
            <el-option label="手工录入" value="manual" />
          </el-select>
          <el-button size="small" :loading="runs.loading.value" @click="runs.reload">刷新</el-button>
          <!-- 不按 `runs.total` 置灰：那个数是**筛选后**的条数，而清空的范围是整个场次 -->
          <el-button size="small" type="danger" plain @click="clearRunsOpen = true">
            清空成绩记录
          </el-button>
        </div>
      </div>

      <p class="cell-sub">点「补录」可手工判分。</p>

      <DataTable
        :rows="runs.rows.value"
        :row-key="(row: JudgeRunOut) => row.id"
        :loading="runs.loading.value"
        :total="runs.total.value"
        :page="runs.page.value"
        :page-size="runs.pageSize.value"
        empty-text="没有匹配的评测记录"
        @update:page="runs.setPage"
        @update:pageSize="runs.setPageSize"
      >
        <template #columns>
          <el-table-column label="选手" prop="player_no" width="110" />
          <el-table-column label="题目" prop="problem" width="100" />
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag :type="runStatusType(row.parse_status)" size="small" effect="plain">
                {{ runStatusLabel(row.parse_status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="得分" width="100" align="right">
            <template #default="{ row }">
              <span v-if="row.score === null || row.score === undefined" class="muted">—</span>
              <span v-else>{{ row.score }}/{{ row.max_score ?? '?' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="来源文件" min-width="220">
            <template #default="{ row }">
              <span v-if="row.source_path" class="mono">{{ row.source_path }}</span>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="说明" min-width="200">
            <template #default="{ row }">
              <span :class="{ 'error-text': row.parse_status === 'unparsed' }">
                {{ row.detail || '—' }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="更新时间" width="160">
            <template #default="{ row }">
              <span class="cell-sub">{{ formatTime(row.updated_at) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="90" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openManual(row, row.problem, row)">
                {{ row.parse_status === 'unparsed' ? '补录' : '修正' }}
              </el-button>
            </template>
          </el-table-column>
        </template>

        <template #empty>
          <p>一条都没有就先确认结果目录写对了，再点「立即重扫」。</p>
        </template>
      </DataTable>
    </div>

    <FormDialog
      v-model="editing"
      title="录入成绩"
      width="480px"
      :submitting="saveManual.pending.value"
      @submit="saveManual.run(undefined)"
    >
      <el-form label-width="90px">
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
          <el-input v-model="form.status" placeholder="例如 AC / WA / 未评测（可留空）" />
        </el-form-item>
      </el-form>

      <!-- 从"未解析"进来时把解析器给的原因摆出来 —— 教师判分前需要知道它读的是什么文件 -->
      <el-alert v-if="manualUnparsed" type="warning" :closable="false" show-icon style="margin-bottom: 10px">
        <template #title>这一格是自动解析失败的，原因：{{ manualSeed?.detail || '（服务端没有给出原因）' }}</template>
        <template #default>
          可到下面「评测记录」按「未解析」筛出同类记录。
        </template>
      </el-alert>

      <template #footer-prepend>
        <el-button
          v-if="form.playerId"
          link
          type="danger"
          :loading="clearScore.pending.value"
          @click="askClear"
        >
          清除这一格
        </el-button>
      </template>
    </FormDialog>

    <!--
      清除某一格 = 硬删除一条评测记录。服务端要 `confirm` 逐字等于考号，
      所以这里不是"是否确定"，而是把考号打一遍。
    -->
    <ConfirmByNameDialog
      v-model="clearCellOpen"
      title="清除这一格成绩"
      :expected="form.playerNo"
      :submitting="clearScore.pending.value"
      confirm-text="清除"
      :detail="
        `将删除 ${form.playerNo} 的「${form.problem}」这一格的成绩记录。` +
        '手工录入的那份会被抹掉，随后自动扫描可以重新接管这一格。' +
        '选手的代码与结果文件都不动 —— 删的只是这条成绩记录。'
      "
      @confirm="clearScore.run(undefined)"
    />

    <!--
      清空成绩记录 = 范围删除。`confirm` 是**场次标识**，不是某一格的名字 ——
      这个动作的范围是整个场次，不跟着记录区上面的状态筛选走，文案要写清楚，
      否则教师会以为"筛了未解析就是只清未解析的"。
    -->
    <ConfirmByNameDialog
      v-model="clearRunsOpen"
      title="清空本场次成绩记录"
      :expected="contest.current?.slug ?? ''"
      :submitting="clearRuns.pending.value"
      confirm-text="清空"
      :detail="
        '将删除本场次的「全部」评测记录（不跟着记录区上面的状态筛选走），' +
        '成绩矩阵随之一起空掉。默认保留手工录入的分数；要连手工分一起清，' +
        '把下面那个勾去掉。'
      "
      @confirm="clearRuns.run(undefined)"
    >
      <el-checkbox v-model="clearKeepManual">保留手工录入的分数（推荐）</el-checkbox>
      <el-alert v-if="!clearKeepManual" type="error" :closable="false" show-icon style="margin-top: 8px">
        <template #title>手工录入的分数会被一起清掉</template>
        <template #default>
          那些分数是评测器给不出结果时教师亲自判的，重扫也恢复不出来 ——
          清掉之后只能重新人工判一遍。
        </template>
      </el-alert>
    </ConfirmByNameDialog>
  </PageShell>
</template>

<style scoped>
.clickable {
  cursor: pointer;
}

.unparsed-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  margin-top: 4px;
}

.scan-errors {
  margin: 4px 0;
  padding-left: 18px;
  max-height: 200px;
  overflow: auto;
}

.list-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.section-title {
  font-size: 15px;
  margin: 0 0 10px;
}

.muted {
  font-size: 12px;
  font-weight: 400;
  color: #909399;
}

.error-text {
  color: #f56c6c;
  font-size: 12px;
}
</style>

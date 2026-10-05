<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { scoreApi } from '@/api'
import type { JudgeRunOut, ScoreCellOut, ScoreRowOut } from '@/api/types'
import { useContestData } from '@/composables/useContestData'
import { useContestStore } from '@/stores/contest'

const contest = useContestStore()

const rescanning = ref(false)

const { data: matrix, loading, error, reload } = useContestData(
  (contestId) => scoreApi.matrix(contestId),
  { interval: 10000 },
)

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

const editing = ref(false)
const submitting = ref(false)
const form = reactive({
  playerId: 0,
  playerNo: '',
  problem: '',
  score: 0,
  maxScore: 100,
  status: '',
})

/**
 * 单元格外观。
 *
 * 这里的核心是**区分"0 分"和"没有成绩"** —— 后端刻意把两者分成不同的
 * parse_status，前端也必须分开呈现。都显示成 0 会让教师以为选手考砸了。
 */
function cellView(cell: ScoreCellOut | undefined): {
  text: string
  type: 'success' | 'danger' | 'warning' | 'info' | 'primary'
  tooltip: string
} {
  if (!cell || cell.parse_status === 'missing') {
    return { text: '—', type: 'info', tooltip: '未收到评测结果' }
  }

  const detail = cell.detail ?? ''

  if (cell.parse_status === 'unparsed') {
    return {
      text: '未解析',
      type: 'danger',
      tooltip: `收到了结果文件但无法解析，需要手工补录。\n原因：${detail}`,
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
  if (detail) parts.push(detail)

  return {
    text: full ? `${cell.score}/${cell.max_score}` : String(cell.score),
    type: cell.parse_status === 'manual' ? 'primary' : isFull ? 'success' : isZero ? 'info' : 'warning',
    tooltip: parts.join('\n'),
  }
}

const unparsedCells = computed(() => {
  const result: Array<{ row: ScoreRowOut; cell: ScoreCellOut }> = []
  for (const row of view.value?.rows ?? []) {
    for (const cell of row.cells) {
      if (cell.parse_status === 'unparsed') result.push({ row, cell })
    }
  }
  return result
})

function openManual(row: ScoreRowOut, problem: string, cell?: ScoreCellOut): void {
  form.playerId = row.player_id
  form.playerNo = row.player_no
  form.problem = problem
  form.score = cell?.score ?? 0
  form.maxScore = cell?.max_score ?? 100
  form.status = cell?.status ?? ''
  editing.value = true
}

async function submitManual(): Promise<void> {
  if (!contest.currentId) return
  submitting.value = true
  try {
    await scoreApi.setManual(contest.currentId, {
      player_id: form.playerId,
      problem: form.problem,
      score: form.score,
      max_score: form.maxScore,
      status: form.status || null,
    })
    ElMessage.success('已保存。手工录入的成绩不会被自动扫描覆盖。')
    editing.value = false
    await reload()
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    submitting.value = false
  }
}

async function clearManual(playerId: number, problem: string): Promise<void> {
  if (!contest.currentId) return
  try {
    await scoreApi.clear(contest.currentId, playerId, problem)
    ElMessage.success('已清除，自动扫描将重新接管这一格')
    editing.value = false
    await reload()
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

async function rescan(): Promise<void> {
  if (!contest.currentId) return
  rescanning.value = true
  try {
    const report = await scoreApi.rescan(contest.currentId)
    const parts = [`解析 ${report.parsed} 条`]
    if (report.unparsed) parts.push(`未解析 ${report.unparsed} 条`)
    if (report.unchanged) parts.push(`未变化 ${report.unchanged} 条`)
    if (report.manual) parts.push(`保留手工录入 ${report.manual} 条`)
    ElMessage.success(parts.join('，'))
    await reload()
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    rescanning.value = false
  }
}

/**
 * 原始记录：每一格成绩是从哪个文件、用哪个解析器、为什么失败。
 *
 * 矩阵只告诉你"这一格是未解析"，但排查需要知道**具体读了哪个文件、为什么读不懂**。
 * 没有这个视图的话，教师只能自己去翻 judge_result 目录猜。
 */
const runsVisible = ref(false)
const runsLoading = ref(false)
const runs = ref<JudgeRunOut[]>([])
const runsFilter = ref<string>('')

async function openRuns(status = ''): Promise<void> {
  if (!contest.currentId) return
  runsVisible.value = true
  runsFilter.value = status
  await loadRuns()
}

async function loadRuns(): Promise<void> {
  if (!contest.currentId) return
  runsLoading.value = true
  try {
    runs.value = await scoreApi.runs(contest.currentId, runsFilter.value || undefined)
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    runsLoading.value = false
  }
}

function runStatusType(status: string): 'success' | 'danger' | 'primary' | 'info' {
  if (status === 'ok') return 'success'
  if (status === 'unparsed') return 'danger'
  if (status === 'manual') return 'primary'
  return 'info'
}

function runStatusLabel(status: string): string {
  const map: Record<string, string> = {
    ok: '已解析',
    unparsed: '未解析',
    manual: '手工录入',
    missing: '无记录',
  }
  return map[status] ?? status
}

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
      if (!cell || cell.parse_status === 'missing') return ''
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
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h2 class="page-title">成绩</h2>
        <p class="page-hint">
          把评测器的输出目录指向
          <code>&lt;数据目录&gt;/judge_result/&lt;场次&gt;/&lt;选手&gt;/&lt;题目&gt;/</code>，
          服务端每 10 秒自动汇总一次。
          <strong>点一下单元格可以手工录分或修正。</strong>
        </p>
      </div>
      <div class="toolbar">
        <el-button size="small" :loading="rescanning" @click="rescan">立即重扫</el-button>
        <el-button size="small" :disabled="!view?.rows.length" @click="openRuns()">
          原始记录
        </el-button>
        <el-button size="small" :disabled="!view?.rows.length" @click="exportCsv">
          导出 CSV
        </el-button>
        <el-button size="small" :loading="loading" @click="reload">刷新</el-button>
      </div>
    </div>

    <el-alert
      v-if="error"
      type="error"
      :closable="false"
      show-icon
      :title="error"
      style="margin-bottom: 12px"
    />

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
            title="只在评测结果里出现过，没在题目清单里登记"
          >
            未登记
          </el-tag>
        </template>
        <template #default="{ row }">
          <el-tooltip
            :content="cellView(row.cells.find((c: ScoreCellOut) => c.problem === column.ident)).tooltip"
          >
            <el-tag
              :type="cellView(row.cells.find((c: ScoreCellOut) => c.problem === column.ident)).type"
              size="small"
              effect="plain"
              class="clickable"
              @click="
                openManual(
                  row,
                  column.ident,
                  row.cells.find((c: ScoreCellOut) => c.problem === column.ident),
                )
              "
            >
              {{ cellView(row.cells.find((c: ScoreCellOut) => c.problem === column.ident)).text }}
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
        <p>还没有任何评测成绩，也没有登记题目。</p>
        <p class="page-hint">
          先在「场次管理」里登记题目清单，或把 LemonLime / Arbiter 的结果输出目录指到
          <code>&lt;数据目录&gt;/judge_result/&lt;场次 slug&gt;/&lt;选手编号&gt;/&lt;题目标识&gt;/</code>
          —— 结果里出现的题目会自动补进矩阵（标注"未登记"）。也可以点右上角「立即重扫」。
        </p>
      </div>
    </el-card>

    <el-dialog v-model="editing" title="录入成绩" width="480px">
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

      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="手工录入的成绩会被标记并永久保留，自动扫描不会覆盖它（连「立即重扫」也不会）。要恢复自动扫描请点「清除」。"
      />

      <template #footer>
        <el-button
          v-if="form.playerId"
          link
          type="danger"
          @click="clearManual(form.playerId, form.problem)"
        >
          清除这一格
        </el-button>
        <el-button @click="editing = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitManual">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="runsVisible" title="成绩原始记录" width="900px">
      <div class="toolbar" style="margin-bottom: 12px">
        <el-select
          v-model="runsFilter"
          size="small"
          placeholder="全部"
          clearable
          style="width: 150px"
          @change="loadRuns"
        >
          <el-option label="未解析（需要处理）" value="unparsed" />
          <el-option label="已解析" value="ok" />
          <el-option label="手工录入" value="manual" />
        </el-select>
        <span class="page-hint" style="margin: 0">
          每一格成绩来自哪个文件、用了哪个解析器、为什么失败
        </span>
      </div>

      <el-table :data="runs" v-loading="runsLoading" size="small" border max-height="420">
        <el-table-column label="选手" prop="player_no" width="100" />
        <el-table-column label="题目" prop="problem" width="90" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="runStatusType(row.parse_status)" size="small" effect="plain">
              {{ runStatusLabel(row.parse_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="得分" width="90" align="right">
          <template #default="{ row }">
            <span v-if="row.score === null || row.score === undefined" class="muted">—</span>
            <span v-else>{{ row.score }}/{{ row.max_score ?? '?' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="来源文件" min-width="240">
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
        <template #empty>
          <div class="empty-block">没有匹配的记录</div>
        </template>
      </el-table>
    </el-dialog>
  </div>
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
</style>

<script setup lang="ts">
/**
 * 自带评测（§12）：题目评测设置、自动评测、排任务 / 重测（全场 / 某人 / 某题）、进度与
 * 失败原因、最近的评测任务（任务即历史，§12.9）、评测机（§12.10，见 JudgeWorkersCard）。
 */
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import { JUDGE_EXPORT_FORMATS, judgingApi, playerApi, problemApi } from '@/api'
import type { JudgeExportFormat } from '@/api'
import { ApiError } from '@/api/client'
import type { BuiltinJudgeProgressOut, JudgeTaskOut, PlayerOut, ProblemOut } from '@/api/types'
import DataTable from '@/components/DataTable.vue'
import JudgeFlagTags from '@/components/JudgeFlagTags.vue'
import JudgeRunDetailDialog from '@/components/JudgeRunDetailDialog.vue'
import JudgeWorkersCard from '@/components/JudgeWorkersCard.vue'
import ScoreTimelineDialog from '@/components/ScoreTimelineDialog.vue'
import { useList } from '@/composables/useList'
import PageShell from '@/components/PageShell.vue'
import SectionCard from '@/components/SectionCard.vue'
import ProblemJudgeDialog from '@/components/ProblemJudgeDialog.vue'
import JudgeExportDialog from '@/components/JudgeExportDialog.vue'
import JudgeImportDialog from '@/components/JudgeImportDialog.vue'
import { usePolling } from '@/composables/usePolling'
import { useContestStore } from '@/stores/contest'
import { formatTime } from '@/utils/format'
import { FLAG_FILTERS, flagParam, flagRowClass } from '@/utils/judgeFlags'
import { TRIGGER_FILTERS, triggerLabel, triggerParams } from '@/utils/judgeTrigger'

const contest = useContestStore()
const router = useRouter()

const problems = ref<ProblemOut[]>([])
const workersCard = ref<InstanceType<typeof JudgeWorkersCard> | null>(null)
const progress = ref<BuiltinJudgeProgressOut | null>(null)
const error = ref<string | null>(null)
const loading = ref(false)

const builtin = computed(() => problems.value.filter((p) => p.judge_mode === 'builtin'))
const ready = computed(() => builtin.value.filter((p) => p.testdata))

async function loadProblems(): Promise<void> {
  const contestId = contest.currentId
  if (!contestId) {
    problems.value = []
    return
  }
  problems.value = (await problemApi.list(contestId)).items
}

const players = ref<PlayerOut[]>([])

async function loadPlayers(): Promise<void> {
  const contestId = contest.currentId
  if (!contestId) {
    players.value = []
    return
  }
  const all: PlayerOut[] = []
  for (let offset = 0; ; ) {
    const page = await playerApi.list(contestId, { limit: 500, offset })
    all.push(...page.items)
    offset += page.items.length
    if (!page.items.length || offset >= page.total) break
  }
  players.value = all
}

async function loadProgress(): Promise<void> {
  const contestId = contest.currentId
  progress.value = contestId ? await judgingApi.progress(contestId) : null
}

async function reload(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    await Promise.all([loadProblems(), loadProgress(), loadPlayers(), workersCard.value?.reload()])
  } catch (err) {
    error.value = (err as Error).message
  } finally {
    loading.value = false
  }
}

watch(() => contest.currentId, () => void reload(), { immediate: true })

usePolling(
  async () => {
    try {
      await loadProgress()
      if (progress.value && (progress.value.queued || progress.value.running)) await tasks.reload()
    } catch {
      // 轮询失败不打断页面；下一轮再取
    }
  },
  { interval: 3000, immediate: false },
)

const percent = computed(() => {
  const p = progress.value
  if (!p || !p.total) return 0
  return Math.round(((p.done + p.failed) / p.total) * 100)
})

// ---- 题目设置 ----

const settingOpen = ref(false)
const settingTarget = ref<ProblemOut | null>(null)

function openSetting(row: ProblemOut): void {
  settingTarget.value = row
  settingOpen.value = true
}

function ioLabel(row: ProblemOut): string {
  if (row.io_mode !== 'file') return '标准输入输出'
  return `${row.input_file || `${row.ident}.in`} / ${row.output_file || `${row.ident}.out`}`
}

// ---- 导入 / 导出题目配置 ----

const importOpen = ref(false)
const exportOpen = ref(false)
const exportFormat = ref<JudgeExportFormat>('syncoj')

function openExport(format: JudgeExportFormat): void {
  exportFormat.value = format
  exportOpen.value = true
}

// ---- 排任务 ----

const target = ref('*')
/** 0 = 全部选手 */
const targetPlayer = ref(0)
const rerun = ref(false)
const queueing = ref(false)

async function queue(problem: string | null, force = false, playerId: number | null = null): Promise<void> {
  const contestId = contest.currentId
  if (!contestId) return
  queueing.value = true
  try {
    const result = await judgingApi.queue(contestId, {
      problem,
      force,
      player_id: playerId,
      rerun: rerun.value,
    })
    ElMessage.success(`已排队 ${result.queued} 个任务`)
    await Promise.all([loadProgress(), tasks.reload()])
  } catch (err) {
    if (err instanceof ApiError && err.code === 'contest_in_progress' && !force) {
      try {
        await ElMessageBox.confirm('场次未结束，将评测当前已回收的代码。', '仍要评测？', {
          type: 'warning',
          confirmButtonText: '仍要评测',
          cancelButtonText: '取消',
        })
      } catch {
        return
      }
      await queue(problem, true, playerId)
      return
    }
    ElMessage.error((err as Error).message)
  } finally {
    queueing.value = false
  }
}

function queueSelected(): void {
  void queue(target.value === '*' ? null : target.value, false, targetPlayer.value || null)
}

// ---- 自动评测（§12.9；智能模式 §12.12）----

type AutoMode = 'off' | 'on_change' | 'smart'
type SmartKey =
  | 'smart_stable_seconds'
  | 'smart_idle_seconds'
  | 'smart_near_end_seconds'
  | 'smart_cooldown_seconds'
  | 'smart_queue_factor'

/** 智能模式的阈值输入：短标签、范围、单位（范围与服务端 JudgeAutoIn 一致） */
const SMART_FIELDS: { key: SmartKey; label: string; min: number; max: number; step: number; unit: string }[] = [
  { key: 'smart_stable_seconds', label: '稳定', min: 30, max: 3600, step: 30, unit: '秒' },
  { key: 'smart_idle_seconds', label: '空闲', min: 60, max: 7200, step: 60, unit: '秒' },
  { key: 'smart_near_end_seconds', label: '结束前', min: 0, max: 7200, step: 60, unit: '秒' },
  { key: 'smart_cooldown_seconds', label: '冷却', min: 0, max: 3600, step: 30, unit: '秒' },
  { key: 'smart_queue_factor', label: '队列倍数', min: 1, max: 1000, step: 1, unit: '×' },
]

const autoMode = ref<AutoMode>('off')
const autoDelay = ref(60)
const smart = ref<Record<SmartKey, number>>({
  smart_stable_seconds: 180,
  smart_idle_seconds: 600,
  smart_near_end_seconds: 900,
  smart_cooldown_seconds: 300,
  smart_queue_factor: 20,
})
const autoSaving = ref(false)

function loadAuto(): void {
  const current = contest.current
  const mode = current?.auto_judge
  autoMode.value = mode === 'on_change' || mode === 'smart' ? mode : 'off'
  autoDelay.value = current?.auto_judge_delay ?? 60
  if (current) for (const field of SMART_FIELDS) smart.value[field.key] = current[field.key] ?? smart.value[field.key]
}

watch(() => contest.current, loadAuto, { immediate: true })

async function saveAuto(): Promise<void> {
  const contestId = contest.currentId
  if (!contestId) return
  autoSaving.value = true
  try {
    const saved = await judgingApi.setAuto(contestId, {
      auto_judge: autoMode.value,
      auto_judge_delay: autoDelay.value,
      ...smart.value,
    })
    const current = contest.current
    if (current) Object.assign(current, saved)
  } catch (err) {
    ElMessage.error((err as Error).message)
    loadAuto()
  } finally {
    autoSaving.value = false
  }
}

// ---- 评测任务（任务即历史）----

/** all = 全部触发方式 */
const taskTrigger = ref('all')
/** 风险标注筛选（§12.11） */
const taskFlag = ref('all')

const tasks = useList<JudgeTaskOut>({
  rowKey: (row) => row.id,
  loader: ({ limit, offset, signal }) => {
    const contestId = contest.currentId
    if (!contestId) return Promise.resolve(null)
    return judgingApi.tasks(
      contestId,
      {
        limit,
        offset,
        ...triggerParams(taskTrigger.value),
        flag: flagParam(taskFlag.value),
      },
      signal,
    )
  },
  pageSize: 20,
  watches: [() => contest.currentId, () => taskTrigger.value, () => taskFlag.value],
})

const TASK_STATUS: Record<string, string> = { queued: '排队', running: '评测中', done: '完成', failed: '失败' }
const OUTCOME_TYPE: Record<string, 'success' | 'warning' | 'danger' | 'info'> = {
  AC: 'success',
  WA: 'danger',
  RE: 'danger',
  TLE: 'warning',
  MLE: 'warning',
  OLE: 'warning',
}

const taskDetailOpen = ref(false)
const taskDetailId = ref<number | null>(null)

function openTask(row: JudgeTaskOut): void {
  taskDetailId.value = row.id
  taskDetailOpen.value = true
}

async function adoptTask(taskId: number): Promise<void> {
  const contestId = contest.currentId
  if (!contestId) return
  try {
    await judgingApi.adopt(contestId, taskId)
    ElMessage.success('已采用')
    taskDetailOpen.value = false
    await tasks.reload()
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

const timelineOpen = ref(false)
const timelineTarget = ref<{ playerId: number; playerNo: string; problem: string | null } | null>(null)

function openTimeline(playerId: number, playerNo: string, problem: string | null): void {
  timelineTarget.value = { playerId, playerNo, problem }
  timelineOpen.value = true
}

function openPlayerTimeline(): void {
  const player = players.value.find((item) => item.id === targetPlayer.value)
  if (player) openTimeline(player.id, player.player_no, target.value === '*' ? null : target.value)
}

</script>

<template>
  <PageShell title="评测" :error="error" retryable @retry="reload">
    <template #toolbar>
      <el-button @click="router.push({ name: 'scores' })">
        <el-icon><Trophy /></el-icon>查看成绩
      </el-button>
      <el-button :loading="loading" @click="reload">
        <el-icon><Refresh /></el-icon>刷新
      </el-button>
    </template>

    <SectionCard title="题目" :meta="`自带评测 ${builtin.length} / ${problems.length} 道`" flush>
      <template #extra>
        <el-button :disabled="!contest.currentId" @click="importOpen = true">
          <el-icon><Upload /></el-icon>导入题目配置
        </el-button>
        <el-dropdown trigger="click" :disabled="!problems.length" @command="openExport">
          <el-button :disabled="!problems.length">
            <el-icon><Download /></el-icon>导出<el-icon class="el-icon--right"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item v-for="f in JUDGE_EXPORT_FORMATS" :key="f.value" :command="f.value">
                {{ f.label }}
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </template>
    <el-table v-loading="loading" :data="problems">
      <el-table-column label="题目" width="160">
        <template #default="{ row }">
          <div class="mono">{{ row.ident }}</div>
          <div v-if="row.title" class="cell-sub">{{ row.title }}</div>
        </template>
      </el-table-column>
      <el-table-column label="评测方式" width="110">
        <template #default="{ row }">
          <el-tag v-if="row.judge_mode === 'builtin'" size="small" type="primary">自带评测</el-tag>
          <el-tag v-else size="small" type="info" effect="plain">外部评测器</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="时限 / 内存" width="130">
        <template #default="{ row }">
          <template v-if="row.judge_mode === 'builtin'">
            {{ row.time_limit_ms ?? 1000 }} ms / {{ row.memory_limit_mb ?? 256 }} MB
          </template>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="输入输出" min-width="150">
        <template #default="{ row }">
          <span v-if="row.judge_mode === 'builtin'" class="mono">{{ ioLabel(row) }}</span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="数据包" min-width="200">
        <template #default="{ row }">
          <template v-if="row.judge_mode !== 'builtin'"><span class="muted">—</span></template>
          <template v-else-if="row.testdata">
            {{ row.testdata.cases }} 个测试点
            <template v-if="row.testdata.subtasks"> · {{ row.testdata.subtasks }} 个子任务</template>
            <template v-if="row.testdata.has_checker"> · checker</template>
            · 满分 {{ row.testdata.total_score }}
          </template>
          <el-tag v-else size="small" type="danger" effect="plain">缺数据包</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="openSetting(row)">设置</el-button>
          <el-button
            v-if="row.judge_mode === 'builtin'"
            link
            type="primary"
            size="small"
            :disabled="!row.testdata"
            :loading="queueing"
            @click="queue(row.ident)"
          >
            评测本题
          </el-button>
        </template>
      </el-table-column>
      <template #empty>
        <div class="empty-block">
          本场次无题目
          <el-button link type="primary" @click="router.push({ name: 'contests' })">登记题目</el-button>
        </div>
      </template>
    </el-table>
    </SectionCard>

    <SectionCard title="任务" :meta="progress?.total ? `共 ${progress.total} 个` : ''">
      <template #extra>
      <el-select v-model="target" style="width: 180px">
        <el-option label="全部自带评测题目" value="*" />
        <el-option v-for="p in ready" :key="p.id" :label="p.ident" :value="p.ident" />
      </el-select>
      <el-select v-model="targetPlayer" filterable style="width: 180px">
        <el-option label="全部选手" :value="0" />
        <el-option
          v-for="p in players"
          :key="p.id"
          :label="p.name ? `${p.player_no} ${p.name}` : p.player_no"
          :value="p.id"
        />
      </el-select>
      <el-checkbox v-model="rerun">不复用已有结果</el-checkbox>
      <el-button type="primary" :loading="queueing" :disabled="!ready.length" @click="queueSelected">
        评测 / 重测
      </el-button>
      <el-button :disabled="!targetPlayer" @click="openPlayerTimeline">
        <el-icon><TrendCharts /></el-icon>版本与分数
      </el-button>
      </template>

    <div class="auto-row">
      <span class="auto-label">自动评测</span>
      <el-radio-group
        v-model="autoMode"
        size="small"
        :disabled="!contest.currentId || autoSaving"
        aria-label="自动评测"
        @change="saveAuto"
      >
        <el-radio-button value="off">关闭</el-radio-button>
        <el-radio-button value="on_change">每次改动</el-radio-button>
        <el-radio-button value="smart">智能</el-radio-button>
      </el-radio-group>
      <span v-if="autoMode === 'on_change'" class="auto-delay">
        合并
        <el-input-number
          v-model="autoDelay"
          :min="0"
          :max="3600"
          :step="10"
          size="small"
          controls-position="right"
          :disabled="!contest.currentId"
          @change="saveAuto"
        />
        秒内的连续改动
      </span>
    </div>
    <div v-if="autoMode === 'smart'" class="auto-row smart-row">
      <label v-for="field in SMART_FIELDS" :key="field.key" class="auto-delay">
        {{ field.label }}
        <el-input-number
          v-model="smart[field.key]"
          class="smart-input"
          :min="field.min"
          :max="field.max"
          :step="field.step"
          size="small"
          controls-position="right"
          :disabled="!contest.currentId"
          @change="saveAuto"
        />
        {{ field.unit }}
      </label>
    </div>

    <template v-if="progress && progress.total">
      <el-progress :percentage="percent" :status="progress.failed ? 'warning' : undefined" />
      <div class="counts">
        排队 {{ progress.queued }}<template v-if="progress.delayed">（等待改动停止 {{ progress.delayed }}）</template>
        · 评测中 {{ progress.running }} · 完成 {{ progress.done }} · 失败 {{ progress.failed }}
      </div>
      <el-table v-if="progress.failures?.length" :data="progress.failures" max-height="280">
        <el-table-column prop="player_no" label="考号" width="120" />
        <el-table-column prop="problem" label="题目" width="120" />
        <el-table-column prop="error" label="失败原因" />
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="queue(row.problem)">重测本题</el-button>
          </template>
        </el-table-column>
      </el-table>
    </template>
    <div v-else-if="!builtin.length" class="muted">无自带评测题目</div>
    <div v-else-if="!ready.length" class="muted">自带评测题目均缺数据包</div>
    <div v-else class="muted">无评测任务</div>
    </SectionCard>

    <SectionCard title="评测记录" :meta="`共 ${tasks.total.value} 次`" flush>
      <template #extra>
        <el-select v-model="taskFlag" style="width: 130px" aria-label="风险标注">
          <el-option v-for="f in FLAG_FILTERS" :key="f.value" :label="f.label" :value="f.value" />
        </el-select>
        <el-select v-model="taskTrigger" style="width: 140px" aria-label="触发方式">
          <el-option v-for="t in TRIGGER_FILTERS" :key="t.value" :label="t.label" :value="t.value" />
        </el-select>
        <el-button :loading="tasks.loading.value" @click="tasks.reload">
          <el-icon><RefreshRight /></el-icon>刷新
        </el-button>
      </template>
      <DataTable
        :rows="tasks.rows.value"
        :row-key="(row: JudgeTaskOut) => row.id"
        :loading="tasks.loading.value"
        :total="tasks.total.value"
        :page="tasks.page.value"
        :page-size="tasks.pageSize.value"
        :filtered="taskTrigger !== 'all' || taskFlag !== 'all'"
        :row-class="flagRowClass"
        empty-text="无评测记录"
        empty-filtered-text="无匹配的评测记录"
        @update:page="tasks.setPage"
        @update:pageSize="tasks.setPageSize"
      >
        <template #columns>
          <el-table-column label="时间" width="160">
            <template #default="{ row }">
              <span class="cell-sub">{{ formatTime(row.finished_at || row.created_at) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="选手" prop="player_no" width="100" />
          <el-table-column label="题目" prop="problem" width="90" />
          <el-table-column label="触发" width="100">
            <template #default="{ row }">{{ triggerLabel(row.trigger, row.reason) }}</template>
          </el-table-column>
          <el-table-column label="代码" min-width="150">
            <template #default="{ row }">
              <template v-if="row.source_sha256">
                <span class="mono">{{ row.source_sha256.slice(0, 8) }}</span>
                <span class="cell-sub">&nbsp;·&nbsp;{{ row.source_path }}</span>
              </template>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="结果" min-width="200">
            <template #default="{ row }">
              <template v-if="row.status === 'done' && row.score !== null && row.score !== undefined">
                {{ row.score }} / {{ row.max_score }}
                <el-tag :type="OUTCOME_TYPE[row.outcome ?? ''] ?? 'info'" size="small" effect="light" class="tag">
                  {{ row.outcome }}
                </el-tag>
                <el-tag v-if="row.reused_from" size="small" type="info" effect="plain" class="tag">复用</el-tag>
                <el-tag v-if="row.in_matrix" size="small" type="primary" effect="plain" class="tag">计入成绩</el-tag>
                <JudgeFlagTags :warning="row.warning_count" :risk="row.risk_count" class="tag" />
              </template>
              <span v-else-if="row.status === 'done' && row.outcome === 'missing'" class="muted">未交</span>
              <span v-else-if="row.status === 'failed'" class="error-text">{{ row.error || '失败' }}</span>
              <span v-else class="muted">
                {{ TASK_STATUS[row.status] ?? row.status
                }}<template v-if="row.not_before && row.status === 'queued'">（{{ formatTime(row.not_before).slice(11) }} 后）</template>
              </span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="130" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" :disabled="!row.has_result" @click="openTask(row)">
                明细
              </el-button>
              <el-button
                link
                type="primary"
                size="small"
                @click="openTimeline(row.player_id, row.player_no, row.problem)"
              >
                版本
              </el-button>
            </template>
          </el-table-column>
        </template>
      </DataTable>
    </SectionCard>

    <JudgeWorkersCard ref="workersCard" />

    <ProblemJudgeDialog v-model="settingOpen" :problem="settingTarget" @saved="loadProblems" />
    <JudgeImportDialog v-model="importOpen" :contest-id="contest.currentId" @imported="loadProblems" />
    <JudgeExportDialog
      v-model="exportOpen"
      :contest-id="contest.currentId"
      :problems="problems"
      :format="exportFormat"
    />
    <JudgeRunDetailDialog
      v-model="taskDetailOpen"
      :contest-id="contest.currentId"
      :run-id="null"
      :task-id="taskDetailId"
      @adopt="(task) => adoptTask(task.id)"
    />
    <ScoreTimelineDialog
      v-model="timelineOpen"
      :contest-id="contest.currentId"
      :player-id="timelineTarget?.playerId ?? null"
      :player-no="timelineTarget?.playerNo ?? ''"
      :problem="timelineTarget?.problem ?? null"
      @changed="tasks.reload"
    />
  </PageShell>
</template>

<style scoped>
.auto-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 16px;
  margin-bottom: 12px;
}
.auto-label {
  color: var(--el-text-color-regular);
  font-size: 13px;
}
.smart-row {
  gap: 12px 20px;
}
.smart-input {
  width: 110px;
}
.auto-delay {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--el-text-color-regular);
  font-size: 13px;
}
.error-text {
  color: var(--sj-danger);
}
.tag {
  margin-left: 6px;
}
.counts {
  margin: 6px 0 10px;
  color: var(--el-text-color-regular);
}
.muted {
  color: var(--el-text-color-secondary);
}
.mono {
  font-family: var(--el-font-family-mono, monospace);
}
.cell-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.empty-block {
  padding: 16px 0;
}
</style>

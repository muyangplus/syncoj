<script setup lang="ts">
/**
 * 一位选手的代码版本与分数 timeline（§12.9）。
 *
 * 给了 `problem`：一张大图 + 版本表（明细 / 重测此版本 / 采用此版本成绩）。
 * 没给：每道题一张小图（小多图，各自的满分做纵轴），点题目名进入那道题。
 */
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { judgingApi } from '@/api'
import { ApiError } from '@/api/client'
import type { SourceVersionOut } from '@/api/types'
import JudgeRunDetailDialog from '@/components/JudgeRunDetailDialog.vue'
import ScoreTimelineChart from '@/components/ScoreTimelineChart.vue'
import { usePolling } from '@/composables/usePolling'
import { formatBytes, formatTime } from '@/utils/format'
import { triggerLabel } from '@/utils/judgeTrigger'

const props = defineProps<{
  modelValue: boolean
  contestId: number | null
  playerId: number | null
  playerNo: string
  /** null = 全部题目 */
  problem: string | null
}>()
const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  /** 采用了某版成绩：成绩矩阵要刷新 */
  (e: 'changed'): void
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const versions = ref<SourceVersionOut[]>([])
const loading = ref(false)
/** 当前看的题；null = 小多图 */
const focus = ref<string | null>(null)
const selectedId = ref<number | null>(null)

async function load(): Promise<void> {
  if (!props.contestId || !props.playerId) return
  const all: SourceVersionOut[] = []
  for (let offset = 0; ; ) {
    const page = await judgingApi.versions(props.contestId, { player_id: props.playerId, offset })
    all.push(...page.items)
    offset += page.items.length
    if (!page.items.length || offset >= page.total) break
  }
  versions.value = all
}

async function reload(): Promise<void> {
  loading.value = true
  try {
    await load()
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.modelValue, props.playerId, props.problem] as const,
  ([open]) => {
    if (!open) return
    focus.value = props.problem
    selectedId.value = null
    versions.value = []
    void reload()
  },
  { immediate: true },
)

/** 有任务在排队或在评时跟着刷新 */
const busy = computed(() =>
  versions.value.some((v) => v.judge && (v.judge.status === 'queued' || v.judge.status === 'running')),
)
usePolling(
  async () => {
    if (!props.modelValue || !busy.value) return
    try {
      await load()
    } catch {
      // 下一轮再取
    }
  },
  { interval: 4000, immediate: false },
)

const groups = computed(() => {
  const map = new Map<string, SourceVersionOut[]>()
  for (const v of versions.value) {
    if (!v.problem) continue
    if (!map.has(v.problem)) map.set(v.problem, [])
    map.get(v.problem)!.push(v)
  }
  return Array.from(map.entries())
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([problem, items]) => ({ problem, items, latest: latestJudged(items), matrix: items.find((v) => v.in_matrix) }))
})

function latestJudged(items: SourceVersionOut[]): SourceVersionOut | undefined {
  for (let i = items.length - 1; i >= 0; i--) {
    if (items[i].judge?.status === 'done' && items[i].judge?.score !== null) return items[i]
  }
  return undefined
}

const focused = computed(() => versions.value.filter((v) => v.problem === focus.value))
/** 表格新的在前 */
const tableRows = computed(() => [...focused.value].reverse())

const title = computed(() => `${props.playerNo} · ${focus.value ?? '全部题目'} · 版本`)

function select(version: SourceVersionOut): void {
  selectedId.value = version.id
}

function rowClass({ row }: { row: SourceVersionOut }): string {
  return row.id === selectedId.value ? 'is-selected' : ''
}

const STATUS_TYPE: Record<string, 'success' | 'warning' | 'danger' | 'info'> = {
  AC: 'success',
  WA: 'danger',
  RE: 'danger',
  TLE: 'warning',
  MLE: 'warning',
  OLE: 'warning',
}


// ---- 动作 ----

const detailOpen = ref(false)
const detailTaskId = ref<number | null>(null)

function openDetail(version: SourceVersionOut): void {
  if (!version.judge) return
  detailTaskId.value = version.judge.task_id
  detailOpen.value = true
}

const acting = ref<number | null>(null)

async function rejudge(version: SourceVersionOut): Promise<void> {
  if (!props.contestId) return
  acting.value = version.id
  try {
    await judgingApi.queue(props.contestId, { version_id: version.id, rerun: true, force: false })
    ElMessage.success('已排队')
    await load()
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    acting.value = null
  }
}

async function adoptTask(taskId: number, score: number | null | undefined, max: number | null | undefined): Promise<void> {
  if (!props.contestId) return
  try {
    await ElMessageBox.confirm(`成绩矩阵中该格改为 ${score ?? '—'} / ${max ?? '—'}。`, '采用此版本成绩？', {
      type: 'warning',
      confirmButtonText: '采用',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    await judgingApi.adopt(props.contestId, taskId)
    ElMessage.success('已采用')
    emit('changed')
    await load()
  } catch (err) {
    if (err instanceof ApiError && err.code === 'manual_score') {
      ElMessage.warning('该格为手工录入成绩，先在成绩页清除')
      return
    }
    ElMessage.error((err as Error).message)
  }
}

function adopt(version: SourceVersionOut): void {
  if (version.judge) void adoptTask(version.judge.task_id, version.judge.score, version.judge.max_score)
}

function canAdopt(version: SourceVersionOut): boolean {
  return !!version.judge && version.judge.status === 'done' && version.judge.score !== null && !version.in_matrix
}

const selected = computed(() => focused.value.find((v) => v.id === selectedId.value) ?? null)
</script>

<template>
  <el-dialog v-model="visible" :title="title" width="900px" top="6vh">
    <div v-loading="loading" class="body">
      <template v-if="focus === null">
        <div v-if="groups.length" class="multiples">
          <div v-for="group in groups" :key="group.problem" class="multiple">
            <div class="multiple-head">
              <el-button link type="primary" class="mono" @click="focus = group.problem">{{ group.problem }}</el-button>
              <span class="muted">{{ group.items.length }} 版</span>
              <span class="multiple-score">
                <template v-if="group.matrix?.judge">{{ group.matrix.judge.score }} / {{ group.matrix.judge.max_score }}</template>
                <template v-else-if="group.latest?.judge">{{ group.latest.judge.score }} / {{ group.latest.judge.max_score }}</template>
                <template v-else>—</template>
              </span>
            </div>
            <ScoreTimelineChart compact :versions="group.items" @select="(v) => { focus = group.problem; select(v) }" />
          </div>
        </div>
        <el-empty v-else-if="!loading" description="无代码版本" :image-size="64" />
      </template>

      <template v-else>
        <div class="toolbar">
          <el-button v-if="problem === null" link type="primary" @click="focus = null">
            <el-icon><ArrowLeft /></el-icon>全部题目
          </el-button>
          <span class="muted">{{ focused.length }} 个版本</span>
          <span class="legend">
            <span class="legend-item"><i class="dot is-ac" />AC</span>
            <span class="legend-item"><i class="dot is-bad" />WA / RE</span>
            <span class="legend-item"><i class="dot is-limit" />TLE / MLE / OLE</span>
            <span class="legend-item"><i class="dot is-other" />CE / SE</span>
            <span class="legend-item"><i class="dot ring" />计入成绩</span>
            <span class="legend-item"><i class="dot hollow" />未评测</span>
          </span>
        </div>

        <ScoreTimelineChart v-if="focused.length" :versions="focused" :selected-id="selectedId" @select="select" />

        <div v-if="selected" class="selected-bar">
          <span class="mono">{{ selected.sha256.slice(0, 8) }}</span>
          <span>{{ formatTime(selected.received_at) }}</span>
          <span v-if="selected.judge?.score !== null && selected.judge?.score !== undefined">
            {{ selected.judge.score }} / {{ selected.judge.max_score }} {{ selected.judge.outcome }}
          </span>
          <span class="spacer" />
          <el-button size="small" :disabled="!selected.judge?.task_id" @click="openDetail(selected)">明细</el-button>
          <el-button size="small" :loading="acting === selected.id" @click="rejudge(selected)">重测此版本</el-button>
          <el-button size="small" type="primary" :disabled="!canAdopt(selected)" @click="adopt(selected)">
            采用此版本成绩
          </el-button>
        </div>

        <el-table :data="tableRows" max-height="320" :row-class-name="rowClass" @row-click="select">
          <el-table-column label="收到时间" width="180">
            <template #default="{ row }">{{ formatTime(row.received_at) }}</template>
          </el-table-column>
          <el-table-column label="代码" min-width="140">
            <template #default="{ row }">
              <span class="mono">{{ row.sha256.slice(0, 8) }}</span>
              <el-tag v-if="row.current" size="small" effect="plain" class="tag">当前</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="大小" width="90" align="right">
            <template #default="{ row }"><span class="cell-sub">{{ formatBytes(row.size) }}</span></template>
          </el-table-column>
          <el-table-column label="结果" width="190">
            <template #default="{ row }">
              <template v-if="!row.judge"><span class="muted">未评测</span></template>
              <template v-else-if="row.judge.status === 'done' && row.judge.score !== null">
                <span class="score">{{ row.judge.score }} / {{ row.judge.max_score }}</span>
                <el-tag :type="STATUS_TYPE[row.judge.outcome ?? ''] ?? 'info'" size="small" effect="light" class="tag">
                  {{ row.judge.outcome }}
                </el-tag>
                <el-tag v-if="row.in_matrix" size="small" type="primary" effect="plain" class="tag">计入成绩</el-tag>
              </template>
              <template v-else-if="row.judge.status === 'failed'"><span class="error-text">评测失败</span></template>
              <template v-else-if="row.judge.status === 'done'"><span class="muted">未交</span></template>
              <template v-else><span class="muted">{{ row.judge.status === 'running' ? '评测中' : '排队中' }}</span></template>
            </template>
          </el-table-column>
          <el-table-column label="评测" min-width="110">
            <template #default="{ row }">
              <template v-if="row.judge">
                {{ triggerLabel(row.judge.trigger, row.judge.reason) }}<span v-if="row.judge.reused" class="cell-sub"> · 复用</span>
                <span v-if="row.tasks > 1" class="cell-sub"> · {{ row.tasks }} 次</span>
              </template>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="200" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" :disabled="!row.judge" @click.stop="openDetail(row)">明细</el-button>
              <el-button link type="primary" size="small" :loading="acting === row.id" @click.stop="rejudge(row)">
                重测
              </el-button>
              <el-button link type="primary" size="small" :disabled="!canAdopt(row)" @click.stop="adopt(row)">
                采用
              </el-button>
            </template>
          </el-table-column>
          <template #empty>无代码版本</template>
        </el-table>
      </template>
    </div>
    <template #footer>
      <el-button :loading="loading" @click="reload">
        <el-icon><Refresh /></el-icon>刷新
      </el-button>
      <el-button type="primary" @click="visible = false">关闭</el-button>
    </template>

    <JudgeRunDetailDialog
      v-model="detailOpen"
      :contest-id="contestId"
      :run-id="null"
      :task-id="detailTaskId"
      append-to-body
      @adopt="(task) => adoptTask(task.id, task.score, task.max_score)"
    />
  </el-dialog>
</template>

<style scoped>
.body {
  min-height: 160px;
}
.multiples {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 12px;
}
.multiple {
  padding: 10px 12px 4px;
  border: 1px solid var(--sj-border);
  border-radius: var(--sj-radius);
  background: var(--sj-surface);
}
.multiple-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 4px;
}
.multiple-score {
  margin-left: auto;
  font-weight: 600;
  color: var(--sj-text);
  font-variant-numeric: tabular-nums;
}
.toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 6px;
}
.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-left: auto;
  font-size: 12px;
  color: var(--sj-text-2);
}
.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.dot {
  display: inline-block;
  width: 9px;
  height: 9px;
  border-radius: 50%;
}
.dot.is-ac {
  background: var(--sj-success);
}
.dot.is-bad {
  background: var(--sj-danger);
}
.dot.is-limit {
  background: var(--sj-warning);
}
.dot.is-other {
  background: var(--sj-text-3);
}
.dot.ring {
  border: 1.5px solid var(--sj-text);
}
.dot.hollow {
  border: 1.5px solid var(--sj-text-3);
}
.selected-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin: 8px 0;
  padding: 8px 12px;
  border-radius: var(--sj-radius-sm);
  background: var(--sj-surface-2);
  border: 1px solid var(--sj-border);
  color: var(--sj-text);
  font-size: 13px;
}
.spacer {
  flex: 1;
}
.mono {
  font-family: var(--sj-mono);
}
.muted {
  color: var(--sj-text-3);
}
.cell-sub {
  font-size: 12px;
  color: var(--sj-text-3);
}
.score {
  font-variant-numeric: tabular-nums;
}
.tag {
  margin-left: 6px;
}
.error-text {
  color: var(--sj-danger);
}
:deep(.is-selected) td {
  background: var(--sj-primary-soft) !important;
}
</style>

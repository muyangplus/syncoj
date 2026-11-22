<script setup lang="ts">
/**
 * 测试点明细（§12）：逐点状态、用时、内存、信息，子任务，编译信息，源码预检的预警。
 *
 * 两种来源：成绩矩阵的一条评测记录（`runId`），或某个评测任务（`taskId`，历史版本的结果，
 * §12.9）。两者明细形状相同，这里归一成同一个视图。
 */
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import { judgingApi } from '@/api'
import type { JudgeRunDetailOut, JudgeTaskDetailOut } from '@/api/types'
import { formatTime } from '@/utils/format'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    contestId: number | null
    runId: number | null
    /** 给了就看这个评测任务（优先于 runId） */
    taskId?: number | null
  }>(),
  { taskId: null },
)
const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'manual', run: JudgeRunDetailOut): void
  (e: 'rejudge', run: JudgeRunDetailOut): void
  (e: 'timeline', run: JudgeRunDetailOut): void
  (e: 'adopt', task: JudgeTaskDetailOut): void
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const run = ref<JudgeRunDetailOut | null>(null)
const task = ref<JudgeTaskDetailOut | null>(null)
const loading = ref(false)

/** 两种来源归一后的视图 */
const detail = computed(() => {
  if (task.value) {
    const t = task.value
    return {
      player_no: t.player_no,
      problem: t.problem,
      score: t.score,
      max_score: t.max_score,
      status: t.outcome,
      parse_status: null as string | null,
      isolated: t.isolated,
      language: t.language,
      judged_source: t.source_path,
      judged_at: t.judged_at,
      compile_message: t.compile_message,
      precheck_warnings: t.precheck_warnings ?? [],
      subtasks: t.subtasks,
      cases: t.cases,
      truncated: t.truncated,
      sha: t.source_sha256,
    }
  }
  if (run.value) {
    const r = run.value
    return {
      player_no: r.player_no,
      problem: r.problem,
      score: r.score,
      max_score: r.max_score,
      status: r.status,
      parse_status: r.parse_status as string | null,
      isolated: r.isolated,
      language: r.language,
      judged_source: r.judged_source,
      judged_at: r.judged_at,
      compile_message: r.compile_message,
      precheck_warnings: r.precheck_warnings ?? [],
      subtasks: r.subtasks,
      cases: r.cases,
      truncated: r.truncated,
      sha: r.source_sha256,
    }
  }
  return null
})

watch(
  () => [props.modelValue, props.runId, props.taskId] as const,
  async ([open, runId, taskId]) => {
    if (!open || !props.contestId || (!runId && !taskId)) return
    loading.value = true
    run.value = null
    task.value = null
    try {
      if (taskId) task.value = await judgingApi.task(props.contestId, taskId)
      else if (runId) run.value = await judgingApi.run(props.contestId, runId)
    } catch (err) {
      ElMessage.error((err as Error).message)
    } finally {
      loading.value = false
    }
  },
  { immediate: true },
)

const STATUS_TYPE: Record<string, 'success' | 'warning' | 'danger' | 'info'> = {
  AC: 'success',
  WA: 'danger',
  TLE: 'warning',
  MLE: 'warning',
  OLE: 'warning',
  RE: 'danger',
  CE: 'info',
  SE: 'info',
}

function statusType(status: string | null | undefined): 'success' | 'warning' | 'danger' | 'info' {
  return STATUS_TYPE[status ?? ''] ?? 'info'
}

const caseSubtask = computed(() => {
  const map = new Map<string, number>()
  for (const sub of detail.value?.subtasks ?? []) {
    for (const name of sub.cases ?? []) map.set(name, sub.index)
  }
  return map
})
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="detail ? `${detail.player_no} · ${detail.problem}` : '评测明细'"
    width="min(820px, calc(100vw - 32px))"
  >
    <div v-loading="loading" class="body">
      <template v-if="detail">
        <div class="summary">
          <div class="summary-score">
            <span class="score-value">{{ detail.score ?? '—' }}</span>
            <span class="score-max">/ {{ detail.max_score ?? '—' }}</span>
          </div>
          <div class="summary-tags">
            <el-tag :type="statusType(detail.status)" effect="light">{{ detail.status || '—' }}</el-tag>
            <el-tag v-if="detail.parse_status === 'manual'" type="warning" effect="plain">手工录分</el-tag>
            <el-tag v-if="task?.in_matrix" type="primary" effect="plain">计入成绩</el-tag>
            <el-tag v-if="task?.reused_from" type="info" effect="plain">复用 #{{ task.reused_from }}</el-tag>
            <el-tag v-if="detail.isolated === false" type="danger" effect="plain">不隔离</el-tag>
          </div>
        </div>
        <dl class="facts">
          <div>
            <dt>语言</dt>
            <dd>{{ detail.language || '—' }}</dd>
          </div>
          <div>
            <dt>评测代码</dt>
            <dd class="mono">
              {{ detail.judged_source || '—' }}<span v-if="detail.sha" class="muted"> · {{ detail.sha.slice(0, 8) }}</span>
            </dd>
          </div>
          <div>
            <dt>评测时间</dt>
            <dd>{{ detail.judged_at ? formatTime(detail.judged_at) : '—' }}</dd>
          </div>
          <div>
            <dt>沙箱</dt>
            <dd>{{ detail.isolated === false ? '不隔离' : detail.isolated ? '隔离' : '—' }}</dd>
          </div>
        </dl>

        <template v-if="detail.compile_message">
          <div class="section-label">编译信息</div>
          <pre class="compile">{{ detail.compile_message }}</pre>
        </template>

        <template v-if="detail.precheck_warnings.length">
          <div class="section-label">
            源码预警 <span class="muted">{{ detail.precheck_warnings.length }} 条</span>
          </div>
          <ul class="warnings">
            <li v-for="(item, index) in detail.precheck_warnings" :key="`${item.code}-${item.line}-${index}`">
              <div class="warning-head">
                <el-tag size="small" type="warning" effect="plain" class="mono">{{ item.code }}</el-tag>
                <span v-if="item.line" class="muted">第 {{ item.line }} 行</span>
                <span class="warning-message">{{ item.message }}</span>
              </div>
              <code v-if="item.excerpt" class="warning-excerpt">{{ item.excerpt }}</code>
            </li>
          </ul>
        </template>

        <template v-if="detail.subtasks?.length">
          <div class="section-label">子任务</div>
          <div class="subtasks">
            <el-tag
              v-for="sub in detail.subtasks"
              :key="sub.index"
              :type="sub.score === sub.max_score ? 'success' : 'danger'"
              effect="plain"
            >
              #{{ sub.index }}：{{ sub.score }} / {{ sub.max_score }}
            </el-tag>
          </div>
        </template>

        <div class="section-label">
          测试点 <span class="muted">{{ detail.cases?.length ?? 0 }} 个</span>
          <el-tag v-if="detail.truncated" type="warning" effect="plain" size="small">已截断</el-tag>
        </div>
        <el-table :data="detail.cases" max-height="360">
          <el-table-column prop="name" label="测试点" width="120" />
          <el-table-column v-if="detail.subtasks?.length" label="子任务" width="70" align="center">
            <template #default="{ row }">{{ caseSubtask.get(row.name) ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="结果" width="80" align="center">
            <template #default="{ row }">
              <el-tag :type="statusType(row.status)" size="small" effect="light">{{ row.status }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="得分" width="64" align="right" prop="score" />
          <el-table-column label="用时（ms）" width="100" align="right">
            <template #default="{ row }">{{ row.time_ms ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="内存（KB）" width="110" align="right">
            <template #default="{ row }">{{ row.memory_kb ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="信息" min-width="160">
            <template #default="{ row }">
              <span class="message">{{ row.message || '' }}</span>
            </template>
          </el-table-column>
          <template #empty>无测试点明细</template>
        </el-table>
      </template>
    </div>
    <template #footer>
      <template v-if="run">
        <el-button @click="emit('timeline', run)">
          <el-icon><TrendCharts /></el-icon>版本
        </el-button>
        <el-button @click="emit('rejudge', run)">
          <el-icon><RefreshRight /></el-icon>重测
        </el-button>
        <el-button @click="emit('manual', run)">手工录分</el-button>
      </template>
      <el-button
        v-if="task"
        :disabled="!task.has_result || task.in_matrix || task.status !== 'done'"
        @click="emit('adopt', task)"
      >
        采用此版本成绩
      </el-button>
      <el-button type="primary" @click="visible = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.body {
  min-height: 120px;
}
.summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding: 14px 16px;
  border-radius: var(--sj-radius);
  background: var(--sj-surface-2);
  border: 1px solid var(--sj-border);
}
.summary-score {
  display: flex;
  align-items: baseline;
  gap: 6px;
}
.score-value {
  font-size: 26px;
  font-weight: 700;
  color: var(--sj-text);
}
.score-max {
  color: var(--sj-text-3);
}
.summary-tags {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.facts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 10px 16px;
  margin: 14px 0 0;
}
.facts dt {
  font-size: 12px;
  color: var(--sj-text-3);
}
.facts dd {
  margin: 2px 0 0;
  color: var(--sj-text);
  word-break: break-all;
}
.section-label {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 18px 0 8px;
  font-weight: 600;
}
.compile {
  max-height: 200px;
  overflow: auto;
  margin: 0;
  padding: 10px 12px;
  border-radius: var(--sj-radius-sm);
  background: var(--sj-surface-2);
  border: 1px solid var(--sj-border);
  color: var(--sj-text);
  font-family: var(--sj-mono);
  font-size: 12px;
  white-space: pre-wrap;
}
.warnings {
  margin: 0;
  padding: 0;
  list-style: none;
  border: 1px solid var(--sj-border);
  border-radius: var(--sj-radius-sm);
  background: var(--sj-warning-soft);
}
.warnings li {
  padding: 8px 12px;
}
.warnings li + li {
  border-top: 1px solid var(--sj-border);
}
.warning-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 13px;
}
.warning-message {
  color: var(--sj-text);
}
.warning-excerpt {
  display: block;
  margin-top: 4px;
  font-family: var(--sj-mono);
  font-size: 12px;
  color: var(--sj-text-2);
  white-space: pre-wrap;
  word-break: break-all;
}
.mono {
  font-family: var(--sj-mono);
}
.subtasks {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.message {
  white-space: pre-wrap;
  font-size: 12px;
}
.muted {
  color: var(--sj-text-3);
}
</style>

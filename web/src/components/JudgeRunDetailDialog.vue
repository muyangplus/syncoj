<script setup lang="ts">
/**
 * 一条评测记录的测试点明细（§12）：逐点状态、用时、内存、信息，子任务，编译信息。
 */
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import { judgingApi } from '@/api'
import type { JudgeRunDetailOut } from '@/api/types'
import { formatTime } from '@/utils/format'

const props = defineProps<{
  modelValue: boolean
  contestId: number | null
  runId: number | null
}>()
const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'manual', run: JudgeRunDetailOut): void
  (e: 'rejudge', run: JudgeRunDetailOut): void
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const detail = ref<JudgeRunDetailOut | null>(null)
const loading = ref(false)

watch(
  () => [props.modelValue, props.runId] as const,
  async ([open, runId]) => {
    if (!open || !runId || !props.contestId) return
    loading.value = true
    detail.value = null
    try {
      detail.value = await judgingApi.run(props.contestId, runId)
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
    width="820px"
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
            <dd class="mono">{{ detail.judged_source || '—' }}</dd>
          </div>
          <div>
            <dt>评测时间</dt>
            <dd>{{ detail.judged_at ? formatTime(detail.judged_at) : '—' }}</dd>
          </div>
          <div>
            <dt>沙箱</dt>
            <dd>{{ detail.isolated === false ? '不隔离' : detail.isolated ? 'isolate' : '—' }}</dd>
          </div>
        </dl>

        <template v-if="detail.compile_message">
          <div class="section-label">编译信息</div>
          <pre class="compile">{{ detail.compile_message }}</pre>
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
      <el-button v-if="detail" @click="emit('rejudge', detail)">
        <el-icon><RefreshRight /></el-icon>重测
      </el-button>
      <el-button v-if="detail" @click="emit('manual', detail)">手工录分</el-button>
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
.subtasks {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.message {
  white-space: pre-wrap;
  font-size: 12px;
}
</style>

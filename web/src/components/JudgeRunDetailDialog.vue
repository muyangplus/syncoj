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
        <el-descriptions :column="3" size="small" border>
          <el-descriptions-item label="得分">
            <strong>{{ detail.score ?? '—' }}</strong> / {{ detail.max_score ?? '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="结果">
            <el-tag :type="statusType(detail.status)" size="small" effect="plain">
              {{ detail.status || '—' }}
            </el-tag>
            <el-tag v-if="detail.parse_status === 'manual'" size="small" type="warning" style="margin-left: 6px">
              手工录分
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="语言">{{ detail.language || '—' }}</el-descriptions-item>
          <el-descriptions-item label="评测代码">
            <span class="mono">{{ detail.judged_source || '—' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="评测时间">
            {{ detail.judged_at ? formatTime(detail.judged_at) : '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="沙箱">
            <el-tag v-if="detail.isolated === false" size="small" type="danger" effect="plain">不隔离</el-tag>
            <span v-else-if="detail.isolated">isolate</span>
            <span v-else>—</span>
          </el-descriptions-item>
        </el-descriptions>

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
          测试点 {{ detail.cases?.length ?? 0 }} 个
          <span v-if="detail.truncated" class="error-text">（结果文件超长，明细已截断）</span>
        </div>
        <el-table :data="detail.cases" size="small" max-height="360" border>
          <el-table-column prop="name" label="测试点" width="120" />
          <el-table-column v-if="detail.subtasks?.length" label="子任务" width="70" align="center">
            <template #default="{ row }">{{ caseSubtask.get(row.name) ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="结果" width="80" align="center">
            <template #default="{ row }">
              <el-tag :type="statusType(row.status)" size="small" effect="plain">{{ row.status }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="得分" width="64" align="right" prop="score" />
          <el-table-column label="用时 (ms)" width="90" align="right">
            <template #default="{ row }">{{ row.time_ms ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="内存 (KB)" width="100" align="right">
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
      <el-button v-if="detail" @click="emit('rejudge', detail)">重测</el-button>
      <el-button v-if="detail" @click="emit('manual', detail)">手工录分</el-button>
      <el-button type="primary" @click="visible = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.body {
  min-height: 120px;
}
.section-label {
  margin: 14px 0 6px;
  font-weight: 600;
}
.compile {
  max-height: 200px;
  overflow: auto;
  margin: 0;
  padding: 8px;
  background: var(--el-fill-color-light);
  font-family: var(--el-font-family-mono, monospace);
  font-size: 12px;
  white-space: pre-wrap;
}
.subtasks {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.mono {
  font-family: var(--el-font-family-mono, monospace);
}
.message {
  white-space: pre-wrap;
  font-size: 12px;
}
.error-text {
  color: var(--el-color-danger);
  font-weight: normal;
}
</style>

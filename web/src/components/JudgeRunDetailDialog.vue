<script setup lang="ts">
/**
 * 测试点明细（§12）：逐点状态、用时、内存、信息，子任务，编译信息，风险与疑似违规（§12.11，
 * 风险在上），以及这次评测的源码（标注的行高亮，点条目滚到那一行）。
 *
 * 两种来源：成绩矩阵的一条评测记录（`runId`），或某个评测任务（`taskId`，历史版本的结果，
 * §12.9）。两者明细形状相同，这里归一成同一个视图。
 */
import { computed, nextTick, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import { judgingApi } from '@/api'
import { ApiError } from '@/api/client'
import type { JudgeRunDetailOut, JudgeSourceOut, JudgeTaskDetailOut } from '@/api/types'
import { formatTime } from '@/utils/format'
import { problemTypeLabel } from '@/utils/judgeStatus'
import JudgeStatusTag from '@/components/JudgeStatusTag.vue'

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
      risk_events: t.risk_events ?? [],
      subtasks: t.subtasks,
      cases: t.cases,
      truncated: t.truncated,
      sha: t.source_sha256,
      task_id: t.id as number | null,
      problem_type: t.problem_type ?? null,
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
      risk_events: r.risk_events ?? [],
      subtasks: r.subtasks,
      cases: r.cases,
      truncated: r.truncated,
      sha: r.source_sha256,
      task_id: (r.task_id ?? null) as number | null,
      problem_type: r.problem_type ?? null,
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
    source.value = null
    sourceError.value = null
    sourceOpen.value = []
    focusLine.value = null
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

const compileBlock = ref<HTMLElement | null>(null)

/** CE 标签：滚到编译信息（§12.16「点击链接查看编译器的输出」） */
function showCompile(): void {
  compileBlock.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

const caseColumns = computed(() => {
  const cases = detail.value?.cases ?? []
  return {
    ratio: cases.some((c) => c.ratio !== null && c.ratio !== undefined),
    file: cases.some((c) => !!c.file),
    interactor: cases.some((c) => c.interactor_time_ms !== null && c.interactor_time_ms !== undefined),
  }
})

function percent(ratio: number | null | undefined): string {
  return ratio === null || ratio === undefined ? '' : `${Math.round(ratio * 1000) / 10}%`
}

// ---- 风险与疑似违规（§12.11；级别由服务端给）----

interface FlagItem {
  key: string
  level: 'risk' | 'warning'
  code: string
  line: number
  case: string | null
  rule: string
  message: string
  excerpt: string
}

const flags = computed<FlagItem[]>(() => {
  const d = detail.value
  if (!d) return []
  const items: FlagItem[] = []
  d.risk_events.forEach((e, i) =>
    items.push({
      key: `e${i}`,
      level: e.level === 'warning' ? 'warning' : 'risk',
      code: e.code,
      line: e.line ?? 0,
      case: e.case ?? null,
      rule: e.rule ?? '',
      message: e.message ?? '',
      excerpt: e.excerpt ?? '',
    }),
  )
  d.precheck_warnings.forEach((w, i) =>
    items.push({
      key: `w${i}`,
      level: w.level === 'risk' ? 'risk' : 'warning',
      code: w.code,
      line: w.line ?? 0,
      case: null,
      rule: '',
      message: w.message ?? '',
      excerpt: w.excerpt ?? '',
    }),
  )
  return items
})

const risks = computed(() => flags.value.filter((f) => f.level === 'risk'))
const warnings = computed(() => flags.value.filter((f) => f.level === 'warning'))

/** 行号 → 级别（同一行风险优先） */
const marked = computed(() => {
  const map = new Map<number, 'risk' | 'warning'>()
  for (const item of flags.value) {
    if (item.line > 0 && map.get(item.line) !== 'risk') map.set(item.line, item.level)
  }
  return map
})

// ---- 源码 ----

const source = ref<JudgeSourceOut | null>(null)
const sourceLoading = ref(false)
const sourceError = ref<string | null>(null)
const sourceOpen = ref<string[]>([])
const focusLine = ref<number | null>(null)
const codeBox = ref<HTMLElement | null>(null)

const sourceLines = computed(() => (source.value ? source.value.content.replace(/\r\n?/g, '\n').split('\n') : []))

async function loadSource(): Promise<void> {
  const taskId = detail.value?.task_id
  if (source.value || sourceLoading.value || !props.contestId || !taskId) return
  sourceLoading.value = true
  sourceError.value = null
  try {
    source.value = await judgingApi.taskSource(props.contestId, taskId)
  } catch (err) {
    sourceError.value = err instanceof ApiError && err.status === 404 ? '代码不在' : (err as Error).message
  } finally {
    sourceLoading.value = false
  }
}

watch(sourceOpen, (open) => {
  if (open.includes('source')) void loadSource()
})

async function jumpTo(item: FlagItem): Promise<void> {
  if (!item.line || !detail.value?.task_id) return
  if (!sourceOpen.value.includes('source')) sourceOpen.value = [...sourceOpen.value, 'source']
  await loadSource()
  focusLine.value = item.line
  await nextTick()
  const box = codeBox.value
  const row = box?.querySelector<HTMLElement>(`[data-line="${item.line}"]`)
  if (box && row) {
    box.scrollTop = Math.max(0, row.offsetTop - box.clientHeight / 2)
    row.scrollIntoView({ block: 'nearest' })
  }
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
            <JudgeStatusTag :code="detail.status" size="default" :clickable="detail.status === 'CE'" @click="showCompile" />
            <el-tag v-if="detail.problem_type" type="info" effect="plain">{{ problemTypeLabel(detail.problem_type) }}</el-tag>
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
          <div ref="compileBlock" class="section-label">编译信息</div>
          <pre class="compile">{{ detail.compile_message }}</pre>
        </template>

        <template v-for="group in [
          { level: 'risk', label: '风险', items: risks },
          { level: 'warning', label: '疑似违规', items: warnings },
        ]" :key="group.level">
          <template v-if="group.items.length">
            <div class="section-label">
              {{ group.label }} <span class="muted">{{ group.items.length }} 条</span>
            </div>
            <ul class="flags">
              <li
                v-for="item in group.items"
                :key="item.key"
                class="flag"
                :class="[`is-${item.level}`, { 'is-jump': item.line > 0 && detail.task_id }]"
                @click="jumpTo(item)"
              >
                <div class="flag-head">
                  <span class="flag-code mono">{{ item.code }}</span>
                  <span v-if="item.rule" class="flag-code mono">{{ item.rule }}</span>
                  <span v-if="item.line" class="muted">第 {{ item.line }} 行</span>
                  <span v-if="item.case" class="muted">测试点 {{ item.case }}</span>
                  <span class="flag-message">{{ item.message }}</span>
                </div>
                <code v-if="item.excerpt" class="flag-excerpt">{{ item.excerpt }}</code>
              </li>
            </ul>
          </template>
        </template>

        <el-collapse v-if="detail.task_id && detail.sha" v-model="sourceOpen" class="source">
          <el-collapse-item name="source">
            <template #title>
              <span class="source-title">源码</span>
              <span v-if="source" class="muted mono">&nbsp;{{ source.path }} · {{ sourceLines.length }} 行</span>
            </template>
            <div v-loading="sourceLoading" class="source-body">
              <div v-if="sourceError" class="muted">{{ sourceError }}</div>
              <div v-else-if="source" ref="codeBox" class="code">
                <div
                  v-for="(text, index) in sourceLines"
                  :key="index"
                  :data-line="index + 1"
                  class="code-line"
                  :class="[
                    marked.get(index + 1) ? `is-${marked.get(index + 1)}` : '',
                    { 'is-focus': focusLine === index + 1 },
                  ]"
                >
                  <span class="code-no">{{ index + 1 }}</span><span class="code-text">{{ text || ' ' }}</span>
                </div>
              </div>
            </div>
          </el-collapse-item>
        </el-collapse>

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
              <JudgeStatusTag :code="row.status" />
            </template>
          </el-table-column>
          <el-table-column label="得分" width="80" align="right">
            <template #default="{ row }">
              {{ row.score }}<span v-if="row.max_score" class="muted"> / {{ row.max_score }}</span>
            </template>
          </el-table-column>
          <el-table-column v-if="caseColumns.ratio" label="比例" width="72" align="right">
            <template #default="{ row }">{{ percent(row.ratio) }}</template>
          </el-table-column>
          <el-table-column v-if="caseColumns.file" label="答案文件" width="120">
            <template #default="{ row }"><span class="mono">{{ row.file }}</span></template>
          </el-table-column>
          <el-table-column label="用时（ms）" width="100" align="right">
            <template #default="{ row }">{{ row.time_ms ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="内存（KB）" width="110" align="right">
            <template #default="{ row }">{{ row.memory_kb ?? '—' }}</template>
          </el-table-column>
          <el-table-column v-if="caseColumns.interactor" label="交互器（ms）" width="110" align="right">
            <template #default="{ row }">{{ row.interactor_time_ms ?? '—' }}</template>
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
.flags {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.flag {
  padding: 8px 12px;
  border: 1px solid var(--flag-line);
  border-left-width: 3px;
  border-radius: var(--sj-radius-sm);
  background: var(--flag-soft);
}
.flag.is-risk {
  --flag-line: var(--sj-danger-line);
  --flag-soft: var(--sj-danger-soft);
  --flag-fg: var(--sj-danger);
}
.flag.is-warning {
  --flag-line: var(--sj-warning-line);
  --flag-soft: var(--sj-warning-soft);
  --flag-fg: var(--sj-warning);
}
.flag.is-jump {
  cursor: pointer;
}
.flag.is-jump:hover {
  border-color: var(--flag-fg);
}
.flag-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 13px;
}
.flag-code {
  padding: 0 6px;
  border: 1px solid var(--flag-line);
  border-radius: var(--sj-radius-sm);
  color: var(--flag-fg);
  font-size: 12px;
  line-height: 18px;
}
.flag-message {
  color: var(--sj-text);
  word-break: break-word;
}
.flag-excerpt {
  display: block;
  margin-top: 4px;
  font-family: var(--sj-mono);
  font-size: 12px;
  color: var(--sj-text-2);
  white-space: pre-wrap;
  word-break: break-all;
}
.source {
  margin-top: 14px;
}
.source-title {
  font-weight: 600;
}
.source-body {
  min-height: 40px;
}
.code {
  max-height: 360px;
  overflow: auto;
  border: 1px solid var(--sj-border);
  border-radius: var(--sj-radius-sm);
  background: var(--sj-surface-2);
  font-family: var(--sj-mono);
  font-size: 12px;
  line-height: 18px;
}
.code-line {
  display: flex;
  min-width: max-content;
}
.code-line.is-warning {
  background: var(--sj-warning-mark);
}
.code-line.is-risk {
  background: var(--sj-danger-mark);
}
.code-line.is-focus .code-no {
  box-shadow: inset 3px 0 0 var(--sj-primary);
  font-weight: 700;
}
.code-no {
  position: sticky;
  left: 0;
  flex: none;
  width: 44px;
  padding-right: 8px;
  text-align: right;
  color: var(--sj-text-3);
  background: var(--sj-surface-2);
  user-select: none;
}
.code-line.is-warning .code-no {
  color: var(--sj-warning);
}
.code-line.is-risk .code-no {
  color: var(--sj-danger);
}
.code-text {
  padding-right: 12px;
  white-space: pre;
  color: var(--sj-text);
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

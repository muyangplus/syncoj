<script setup lang="ts">
/**
 * 自带评测（§12）：题目评测设置、排任务 / 重测、进度与失败原因、评测机令牌。
 */
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import { JUDGE_EXPORT_FORMATS, judgingApi, problemApi } from '@/api'
import type { JudgeExportFormat } from '@/api'
import { ApiError } from '@/api/client'
import type { BuiltinJudgeProgressOut, JudgeWorkerOut, ProblemOut } from '@/api/types'
import ConfirmByNameDialog from '@/components/ConfirmByNameDialog.vue'
import PageShell from '@/components/PageShell.vue'
import SectionCard from '@/components/SectionCard.vue'
import ProblemJudgeDialog from '@/components/ProblemJudgeDialog.vue'
import JudgeExportDialog from '@/components/JudgeExportDialog.vue'
import JudgeImportDialog from '@/components/JudgeImportDialog.vue'
import { usePolling } from '@/composables/usePolling'
import { useContestStore } from '@/stores/contest'
import { formatTime } from '@/utils/format'

const contest = useContestStore()
const router = useRouter()

const problems = ref<ProblemOut[]>([])
const workers = ref<JudgeWorkerOut[]>([])
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

async function loadWorkers(): Promise<void> {
  workers.value = (await judgingApi.workers()).items
}

async function loadProgress(): Promise<void> {
  const contestId = contest.currentId
  progress.value = contestId ? await judgingApi.progress(contestId) : null
}

async function reload(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    await Promise.all([loadProblems(), loadWorkers(), loadProgress()])
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
      await Promise.all([loadProgress(), loadWorkers()])
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
const queueing = ref(false)

async function queue(problem: string | null, force = false): Promise<void> {
  const contestId = contest.currentId
  if (!contestId) return
  queueing.value = true
  try {
    const result = await judgingApi.queue(contestId, { problem, force })
    ElMessage.success(`已排队 ${result.queued} 个任务`)
    await loadProgress()
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
      await queue(problem, true)
      return
    }
    ElMessage.error((err as Error).message)
  } finally {
    queueing.value = false
  }
}

// ---- 评测机 ----

const ONLINE_MS = 60_000

function workerOnline(row: JudgeWorkerOut): boolean {
  return !!row.last_seen_at && Date.now() - new Date(row.last_seen_at).getTime() < ONLINE_MS
}

const issued = ref<{ name: string; token: string } | null>(null)
const command = computed(() =>
  issued.value
    ? `echo '${issued.value.token}' > judge.token\n` +
      `syncoj-judge --server ${window.location.origin} --token-file judge.token --backend isolate`
    : '',
)

async function createWorker(): Promise<void> {
  let name: string
  try {
    const reply = await ElMessageBox.prompt('评测机名称', '添加评测机', {
      inputPattern: /\S/,
      inputErrorMessage: '名称不能为空',
      confirmButtonText: '签发令牌',
      cancelButtonText: '取消',
    })
    name = reply.value.trim()
  } catch {
    return
  }
  try {
    const created = await judgingApi.createWorker(name)
    issued.value = { name: created.name, token: created.token }
    await loadWorkers()
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

async function copyCommand(): Promise<void> {
  try {
    await navigator.clipboard.writeText(command.value)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('复制失败')
  }
}

const removing = ref<JudgeWorkerOut | null>(null)
const removeOpen = ref(false)

function askRemove(worker: JudgeWorkerOut): void {
  removing.value = worker
  removeOpen.value = true
}

async function confirmRemove(): Promise<void> {
  if (!removing.value) return
  try {
    await judgingApi.removeWorker(removing.value.id, removing.value.name)
    removeOpen.value = false
    await loadWorkers()
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
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
      <el-select v-model="target" style="width: 200px">
        <el-option label="全部自带评测题目" value="*" />
        <el-option v-for="p in ready" :key="p.id" :label="p.ident" :value="p.ident" />
      </el-select>
      <el-button
        type="primary"
        :loading="queueing"
        :disabled="!ready.length"
        @click="queue(target === '*' ? null : target)"
      >
        评测 / 重测
      </el-button>
      </template>

    <template v-if="progress && progress.total">
      <el-progress :percentage="percent" :status="progress.failed ? 'warning' : undefined" />
      <div class="counts">
        排队 {{ progress.queued }} · 评测中 {{ progress.running }} · 完成
        {{ progress.done }} · 失败 {{ progress.failed }}
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

    <SectionCard title="评测机" :meta="`在线 ${workers.filter(workerOnline).length} 台 / 共 ${workers.length} 台`" flush>
      <template #extra>
        <el-button type="primary" plain @click="createWorker">
          <el-icon><Plus /></el-icon>添加评测机
        </el-button>
      </template>
    <el-alert
      v-if="issued"
      type="success"
      :closable="true"
      show-icon
      style="margin: 12px 20px"
      :title="`评测机 ${issued.name} 的令牌仅显示一次`"
      @close="issued = null"
    >
      <pre class="command">{{ command }}</pre>
      <el-button @click="copyCommand">
        <el-icon><DocumentCopy /></el-icon>复制命令
      </el-button>
    </el-alert>
    <el-table :data="workers">
      <el-table-column prop="name" label="名称" min-width="160" />
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag v-if="workerOnline(row)" size="small" type="success">在线</el-tag>
          <el-tag v-else size="small" type="info" effect="plain">离线</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="最近联系" width="170">
        <template #default="{ row }">{{ row.last_seen_at ? formatTime(row.last_seen_at) : '从未' }}</template>
      </el-table-column>
      <el-table-column label="地址" width="140">
        <template #default="{ row }">{{ row.last_seen_ip || '—' }}</template>
      </el-table-column>
      <el-table-column label="签发时间" width="170">
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="80" fixed="right">
        <template #default="{ row }">
          <el-button link type="danger" size="small" @click="askRemove(row)">吊销</el-button>
        </template>
      </el-table-column>
      <template #empty>无评测机</template>
    </el-table>
    </SectionCard>

    <ProblemJudgeDialog v-model="settingOpen" :problem="settingTarget" @saved="loadProblems" />
    <JudgeImportDialog v-model="importOpen" :contest-id="contest.currentId" @imported="loadProblems" />
    <JudgeExportDialog
      v-model="exportOpen"
      :contest-id="contest.currentId"
      :problems="problems"
      :format="exportFormat"
    />
    <ConfirmByNameDialog
      v-model="removeOpen"
      title="吊销评测机？"
      :expected="removing?.name ?? ''"
      confirm-text="吊销"
      detail="令牌立即失效；该评测机未交回的任务 15 分钟后重新排队。"
      @confirm="confirmRemove"
    />
  </PageShell>
</template>

<style scoped>
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
.command {
  margin: 6px 0;
  font-family: var(--el-font-family-mono, monospace);
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}
.empty-block {
  padding: 16px 0;
}
</style>

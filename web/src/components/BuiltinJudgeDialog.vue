<script setup lang="ts">
/**
 * 自带评测（§12）：排任务 / 重测、进度、失败原因、评测机令牌。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { judgingApi, problemApi } from '@/api'
import { ApiError } from '@/api/client'
import type { BuiltinJudgeProgressOut, JudgeWorkerOut, ProblemOut } from '@/api/types'
import ConfirmByNameDialog from '@/components/ConfirmByNameDialog.vue'
import { formatTime } from '@/utils/format'

const props = defineProps<{ modelValue: boolean; contestId: number | null }>()
const emit = defineEmits<{ (e: 'update:modelValue', value: boolean): void }>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const problems = ref<ProblemOut[]>([])
const builtin = computed(() => problems.value.filter((p) => p.judge_mode === 'builtin'))
const target = ref<string>('')
const progress = ref<BuiltinJudgeProgressOut | null>(null)
const workers = ref<JudgeWorkerOut[]>([])
const queueing = ref(false)
let timer: number | undefined

const percent = computed(() => {
  const p = progress.value
  if (!p || !p.total) return 0
  return Math.round(((p.done + p.failed) / p.total) * 100)
})

async function refresh(): Promise<void> {
  if (!props.contestId) return
  try {
    progress.value = await judgingApi.progress(props.contestId)
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

async function loadAll(): Promise<void> {
  if (!props.contestId) return
  try {
    problems.value = (await problemApi.list(props.contestId)).items
    workers.value = (await judgingApi.workers()).items
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
  await refresh()
}

watch(
  () => props.modelValue,
  (open) => {
    window.clearInterval(timer)
    if (!open) return
    void loadAll()
    timer = window.setInterval(() => void refresh(), 2000)
  },
  { immediate: true },
)
onBeforeUnmount(() => window.clearInterval(timer))

async function queue(force = false): Promise<void> {
  if (!props.contestId) return
  queueing.value = true
  try {
    const result = await judgingApi.queue(props.contestId, { problem: target.value || null, force })
    ElMessage.success(`已排 ${result.queued} 个任务`)
    await refresh()
  } catch (err) {
    if (err instanceof ApiError && err.code === 'contest_in_progress' && !force) {
      try {
        await ElMessageBox.confirm('场次还在进行：现在评的是考到一半的代码。', '仍要评测？', {
          type: 'warning',
          confirmButtonText: '仍要评测',
          cancelButtonText: '取消',
        })
      } catch {
        return
      }
      await queue(true)
      return
    }
    ElMessage.error((err as Error).message)
  } finally {
    queueing.value = false
  }
}

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
    workers.value = (await judgingApi.workers()).items
    await ElMessageBox.alert(
      `令牌（只显示这一次）：\n${created.token}\n\n` +
        `echo '${created.token}' > token && syncoj-judge --server ${window.location.origin} --token-file token --backend isolate`,
      `评测机 ${created.name}`,
      { confirmButtonText: '已抄下', customClass: 'mono-message' },
    )
  } catch (err) {
    if (err !== 'cancel' && err !== 'close') ElMessage.error((err as Error).message)
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
    workers.value = (await judgingApi.workers()).items
    removeOpen.value = false
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}
</script>

<template>
  <el-dialog v-model="visible" title="自带评测" width="760px" :close-on-click-modal="false">
    <div class="row">
      <el-select v-model="target" size="small" style="width: 200px" placeholder="全部自带评测的题">
        <el-option label="全部自带评测的题" value="" />
        <el-option v-for="p in builtin" :key="p.id" :label="p.ident" :value="p.ident" />
      </el-select>
      <el-button size="small" type="primary" :loading="queueing" :disabled="!builtin.length" @click="queue()">
        评测 / 重测
      </el-button>
      <span v-if="!builtin.length" class="muted">没有设成自带评测的题</span>
    </div>

    <template v-if="progress && progress.total">
      <el-progress :percentage="percent" :status="progress.failed ? 'warning' : undefined" />
      <div class="counts">
        排队 {{ progress.queued }} · 在评 {{ progress.running }} · 完成 {{ progress.done }} · 失败
        {{ progress.failed }}
      </div>
      <el-table v-if="progress.failures?.length" :data="progress.failures" size="small" max-height="200">
        <el-table-column prop="player_no" label="考号" width="110" />
        <el-table-column prop="problem" label="题目" width="100" />
        <el-table-column prop="error" label="原因" />
      </el-table>
    </template>

    <el-divider content-position="left">评测机</el-divider>
    <el-table :data="workers" size="small" max-height="200">
      <el-table-column prop="name" label="名称" width="140" />
      <el-table-column label="最近联系" width="170">
        <template #default="{ row }">{{ row.last_seen_at ? formatTime(row.last_seen_at) : '—' }}</template>
      </el-table-column>
      <el-table-column prop="last_seen_ip" label="地址" width="130" />
      <el-table-column label="操作" width="80">
        <template #default="{ row }">
          <el-button link type="danger" size="small" @click="askRemove(row)">吊销</el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-button size="small" style="margin-top: 8px" @click="createWorker">添加评测机</el-button>

    <ConfirmByNameDialog
      v-model="removeOpen"
      title="吊销评测机"
      :expected="removing?.name ?? ''"
      confirm-text="吊销"
      detail="它手上没交的任务会在 15 分钟后回到队列。"
      @confirm="confirmRemove"
    />
  </el-dialog>
</template>

<style scoped>
.row {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
}
.counts {
  margin: 6px 0 10px;
  color: var(--el-text-color-regular);
}
.muted {
  color: var(--el-text-color-secondary);
}
</style>

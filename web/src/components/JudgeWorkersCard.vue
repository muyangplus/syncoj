<script setup lang="ts">
/**
 * 「评测」页的评测机卡片（§12.10）：「评测服务」总开关与排队数，每台的领任务开关、并发数、
 * 状态与正在评的任务、停止 / 重启 / 改名 / 吊销，添加评测机（一行安装命令）与本机评测机启停。
 */
import { computed, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { judgingApi } from '@/api'
import type { JudgeServiceOut, JudgeWorkerOut, LocalJudgeOut } from '@/api/types'
import ConfirmByNameDialog from '@/components/ConfirmByNameDialog.vue'
import JudgeWorkerAddDialog from '@/components/JudgeWorkerAddDialog.vue'
import SectionCard from '@/components/SectionCard.vue'
import { usePolling } from '@/composables/usePolling'
import { formatTime } from '@/utils/format'

const workers = ref<JudgeWorkerOut[]>([])
const local = ref<LocalJudgeOut | null>(null)
const service = ref<JudgeServiceOut | null>(null)
const loading = ref(false)

async function load(): Promise<void> {
  const [list, localStatus, serviceStatus] = await Promise.all([
    judgingApi.workers(),
    judgingApi.localJudge(),
    judgingApi.service(),
  ])
  workers.value = list.items
  local.value = localStatus
  service.value = serviceStatus
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

usePolling(
  async () => {
    try {
      await load()
    } catch {
      // 轮询失败不打断页面；下一轮再取
    }
  },
  { interval: 3000 },
)

defineExpose({ reload })

const onlineCount = computed(() => workers.value.filter((w) => w.online).length)

type TagType = 'success' | 'warning' | 'danger' | 'info' | 'primary'

function stateOf(row: JudgeWorkerOut): { label: string; type: TagType; plain: boolean } {
  if (!row.online) return { label: '离线', type: 'info', plain: true }
  if (!row.enabled) return { label: '暂停', type: 'warning', plain: false }
  if (row.running?.length) return { label: '忙碌', type: 'primary', plain: false }
  return { label: '在线', type: 'success', plain: false }
}

const DESIRED_LABEL: Record<string, string> = { stop: '停止中', restart: '重启中' }

// ---- 评测服务总开关 ----

const serviceBusy = ref(false)

async function toggleService(enabled: boolean): Promise<void> {
  const current = service.value
  if (!enabled && current) {
    try {
      await ElMessageBox.confirm(
        `排队 ${current.queued} 个任务停止分发，评测中 ${current.running} 个照常评完。`,
        '关闭评测服务？',
        { type: 'warning', confirmButtonText: '关闭', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
  }
  serviceBusy.value = true
  try {
    service.value = await judgingApi.setService({ enabled })
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    serviceBusy.value = false
  }
}

// ---- 添加 ----

const addOpen = ref(false)

// ---- 本机评测机 ----

const localBusy = ref(false)

async function toggleLocal(): Promise<void> {
  if (!local.value) return
  localBusy.value = true
  try {
    local.value = local.value.running ? await judgingApi.stopLocalJudge() : await judgingApi.startLocalJudge()
    workers.value = (await judgingApi.workers()).items
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    localBusy.value = false
  }
}

// ---- 行操作 ----

const acting = ref<number | null>(null)

async function act(row: JudgeWorkerOut, action: () => Promise<JudgeWorkerOut>, done: string): Promise<void> {
  acting.value = row.id
  try {
    const updated = await action()
    const index = workers.value.findIndex((w) => w.id === updated.id)
    if (index >= 0) workers.value[index] = updated
    ElMessage.success(done)
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    acting.value = null
  }
}

function toggleEnabled(row: JudgeWorkerOut, enabled: boolean): void {
  void act(row, () => judgingApi.updateWorker(row.id, { enabled }), enabled ? '已恢复' : '已暂停')
}

// ---- 并发数：改完 600 ms 再提交，评测机评完手上的任务后以新值重启 ----

const slotDraft = reactive(new Map<number, number>())
const slotTimers = new Map<number, ReturnType<typeof setTimeout>>()

function slotsValue(row: JudgeWorkerOut): number {
  return slotDraft.get(row.id) ?? row.desired_slots ?? row.slots ?? 1
}

function slotsEditable(row: JudgeWorkerOut): boolean {
  return row.slots_control && row.backend === 'isolate'
}

function onSlots(row: JudgeWorkerOut, value: number | undefined): void {
  if (!value) return
  slotDraft.set(row.id, value)
  const timer = slotTimers.get(row.id)
  if (timer) clearTimeout(timer)
  slotTimers.set(
    row.id,
    setTimeout(() => {
      slotTimers.delete(row.id)
      const slots = slotDraft.get(row.id)
      if (!slots) return
      void act(row, () => judgingApi.updateWorker(row.id, { slots }), `并发数 ${slots}`).finally(() =>
        slotDraft.delete(row.id),
      )
    }, 600),
  )
}

async function rename(row: JudgeWorkerOut): Promise<void> {
  let name: string
  try {
    const reply = await ElMessageBox.prompt('名称', '改名', {
      inputValue: row.name,
      inputPattern: /\S/,
      inputErrorMessage: '名称不能为空',
      confirmButtonText: '保存',
      cancelButtonText: '取消',
    })
    name = reply.value.trim()
  } catch {
    return
  }
  if (name === row.name) return
  await act(row, () => judgingApi.updateWorker(row.id, { name }), '已改名')
}

async function command(row: JudgeWorkerOut, kind: 'stop' | 'restart'): Promise<void> {
  const busy = row.running?.length ? `评完手上 ${row.running?.length} 个任务后` : ''
  try {
    await ElMessageBox.confirm(
      kind === 'stop' ? `${busy}退出，不会自动拉起。` : `${busy}以同样的参数重启。`,
      kind === 'stop' ? `停止 ${row.name}？` : `重启 ${row.name}？`,
      {
        type: kind === 'stop' ? 'warning' : 'info',
        confirmButtonText: kind === 'stop' ? '停止' : '重启',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }
  await act(
    row,
    () => (kind === 'stop' ? judgingApi.stopWorker(row.id) : judgingApi.restartWorker(row.id)),
    kind === 'stop' ? '已下令停止' : '已下令重启',
  )
}

function onMore(row: JudgeWorkerOut, cmd: string): void {
  if (cmd === 'restart' || cmd === 'stop') void command(row, cmd)
  else if (cmd === 'rename') void rename(row)
  else if (cmd === 'revoke') askRemove(row)
}

const removing = ref<JudgeWorkerOut | null>(null)
const removeOpen = ref(false)
const removeSubmitting = ref(false)

function askRemove(worker: JudgeWorkerOut): void {
  removing.value = worker
  removeOpen.value = true
}

const removeImpact = computed(() => {
  const row = removing.value
  if (!row) return []
  const lines: string[] = []
  if (row.running?.length) lines.push(`正在评的 ${row.running?.length} 个任务立即退回队列`)
  if (row.local) lines.push('本机评测机进程一并停止')
  return lines
})

async function confirmRemove(): Promise<void> {
  if (!removing.value) return
  removeSubmitting.value = true
  try {
    await judgingApi.removeWorker(removing.value.id, removing.value.name)
    removeOpen.value = false
    await load()
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    removeSubmitting.value = false
  }
}
</script>

<template>
  <SectionCard title="评测机" :meta="`在线 ${onlineCount} / ${workers.length}`" flush>
    <template #extra>
      <span v-if="service" class="service">
        <el-switch
          :model-value="service.enabled"
          :loading="serviceBusy"
          active-text="评测服务"
          @change="(value: string | number | boolean) => toggleService(Boolean(value))"
        />
        <span class="service-count">排队 {{ service.queued }}</span>
      </span>
      <span v-if="local && !local.supported" class="local-off">
        <el-button disabled>
          <el-icon><Monitor /></el-icon>本机评测机
        </el-button>
        <el-tag size="small" type="info" effect="plain">{{ local.reason }}</el-tag>
      </span>
      <el-button
        v-else-if="local"
        :type="local.running ? 'default' : 'primary'"
        :plain="!local.running"
        :loading="localBusy"
        @click="toggleLocal"
      >
        <el-icon><component :is="local.running ? 'VideoPause' : 'VideoPlay'" /></el-icon>
        {{ local.running ? '停止本机评测机' : '启动本机评测机' }}
      </el-button>
      <el-button type="primary" @click="addOpen = true">
        <el-icon><Plus /></el-icon>添加评测机
      </el-button>
    </template>

    <el-table v-loading="loading" :data="workers" row-key="id">
      <el-table-column label="领任务" width="80" align="center">
        <template #default="{ row }">
          <el-switch
            :model-value="row.enabled"
            size="small"
            :loading="acting === row.id"
            :aria-label="`${row.name} 领任务`"
            @change="(value: string | number | boolean) => toggleEnabled(row, Boolean(value))"
          />
        </template>
      </el-table-column>
      <el-table-column label="名称" min-width="150">
        <template #default="{ row }">
          <div class="name">
            {{ row.name }}
            <el-tag v-if="row.local" size="small" type="primary" effect="plain">本机</el-tag>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="状态" min-width="170">
        <template #default="{ row }">
          <div class="tags">
            <el-tag size="small" :type="stateOf(row).type" :effect="stateOf(row).plain ? 'plain' : 'light'">
              {{ stateOf(row).label }}
            </el-tag>
            <el-tag v-if="DESIRED_LABEL[row.desired]" size="small" type="warning" effect="plain">
              {{ DESIRED_LABEL[row.desired] }}
            </el-tag>
          </div>
          <div v-for="task in row.running" :key="task.id" class="cell-sub mono">
            #{{ task.id }} {{ task.player_no }} · {{ task.problem }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="后端" width="140">
        <template #default="{ row }">
          <el-tag
            v-if="row.backend"
            size="small"
            :type="row.isolated === false ? 'warning' : 'info'"
            effect="plain"
          >
            {{ row.backend }}<template v-if="row.isolated === false"> · 不隔离</template>
          </el-tag>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="主机 / 平台" min-width="150">
        <template #default="{ row }">
          <template v-if="row.hostname">
            <div class="mono">{{ row.hostname }}</div>
            <div class="cell-sub">
              {{ row.platform }}
            </div>
          </template>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="并发" width="130">
        <template #default="{ row }">
          <template v-if="slotsEditable(row)">
            <el-input-number
              :model-value="slotsValue(row)"
              :min="1"
              :max="64"
              size="small"
              controls-position="right"
              class="slots-input"
              :aria-label="`${row.name} 并发数`"
              @change="(value: number | undefined) => onSlots(row, value)"
            />
            <div
              v-if="row.desired_slots && row.slots && row.desired_slots !== row.slots"
              class="cell-sub"
            >
              当前 {{ row.slots }}
            </div>
          </template>
          <template v-else>
            <span class="mono">{{ row.slots ?? '—' }}</span>
            <el-tag
              v-if="row.slots_control && row.backend !== 'isolate'"
              size="small"
              type="info"
              effect="plain"
              class="slots-tag"
            >
              仅 isolate
            </el-tag>
          </template>
        </template>
      </el-table-column>
      <el-table-column label="版本" width="90">
        <template #default="{ row }">
          <span v-if="row.version" class="mono">{{ row.version }}</span>
          <el-tag v-else-if="row.last_seen_at" size="small" type="info" effect="plain">旧版</el-tag>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="IP" width="130">
        <template #default="{ row }">
          <span class="mono">{{ row.last_seen_ip || '—' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="最近心跳" width="160">
        <template #default="{ row }">
          <span class="cell-sub">{{ row.last_seen_at ? formatTime(row.last_seen_at) : '从未' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="80" fixed="right">
        <template #default="{ row }">
          <el-dropdown trigger="click" @command="(cmd: string) => onMore(row, cmd)">
            <el-button link type="primary" size="small">
              更多<el-icon class="el-icon--right"><ArrowDown /></el-icon>
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="restart" :disabled="!row.online || !row.remote_control">
                  重启
                </el-dropdown-item>
                <el-dropdown-item command="stop" :disabled="!row.online || !row.remote_control">
                  停止
                </el-dropdown-item>
                <el-dropdown-item command="rename">改名</el-dropdown-item>
                <el-dropdown-item command="revoke" divided class="danger-item">吊销</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </template>
      </el-table-column>
      <template #empty>
        <div class="empty-block">
          无评测机
          <el-button link type="primary" @click="addOpen = true">添加评测机</el-button>
        </div>
      </template>
    </el-table>

    <JudgeWorkerAddDialog v-model="addOpen" @created="() => void load()" />
    <ConfirmByNameDialog
      v-model="removeOpen"
      title="吊销评测机？"
      :expected="removing?.name ?? ''"
      confirm-text="吊销"
      detail="令牌立即失效。"
      :impact="removeImpact"
      :submitting="removeSubmitting"
      @confirm="confirmRemove"
    />
  </SectionCard>
</template>

<style scoped>
.service {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin-right: 4px;
}
.service-count {
  font-size: 13px;
  color: var(--sj-text-2);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.slots-input {
  width: 92px;
}
.slots-tag {
  margin-left: 6px;
}
.local-off {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.name {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  font-weight: 500;
}
.tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.mono {
  font-family: var(--sj-mono);
  font-size: 12.5px;
}
.cell-sub {
  margin-top: 2px;
  font-size: 12px;
  color: var(--sj-text-3);
}
.muted {
  color: var(--sj-text-3);
}
.empty-block {
  padding: 16px 0;
}
</style>

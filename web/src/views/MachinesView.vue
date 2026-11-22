<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import { agentApi, bootstrapKeyApi, machineApi, settingsApi } from '@/api'
import { GLOBAL_CONFIRM } from '@/api/endpoints'
import type {
  AgentBindingOut,
  AnnounceStatusOut,
  BindingSuggestionOut,
  BindResultOut,
  BoundMachineOut,
  BootstrapKeyIssuedOut,
  BootstrapKeyOut,
  CloneAlertOut,
  PendingMachineOut,
  RuntimeSettingsOut,
} from '@/api/types'
import ConfirmByNameDialog from '@/components/ConfirmByNameDialog.vue'
import DataTable from '@/components/DataTable.vue'
import DiagnosticsDialog from '@/components/DiagnosticsDialog.vue'
import FormDialog from '@/components/FormDialog.vue'
import PageShell from '@/components/PageShell.vue'
import SectionCard from '@/components/SectionCard.vue'
import BindingSuggestionsDialog from '@/components/BindingSuggestionsDialog.vue'
import RosterPersonPicker from '@/components/RosterPersonPicker.vue'
import { useList } from '@/composables/useList'
import { useMutation } from '@/composables/useMutation'
import { usePolling } from '@/composables/usePolling'
import { describeError } from '@/api/crud'
import { useContestStore } from '@/stores/contest'
import { formatSince, formatTime } from '@/utils/format'

const contest = useContestStore()
const route = useRoute()
const router = useRouter()

// --------------------------------------------------------------------------- //
// 待配对的机器
// --------------------------------------------------------------------------- //

const pending = useList<PendingMachineOut>({
  rowKey: (row) => row.id,
  loader: ({ limit, offset, signal }) => machineApi.pending({ limit, offset }, signal),
  // 8 秒一轮：教师正站在机器前面，机器刚心跳过，他需要很快看到它出现在列表里
  interval: 8000,
})

const alerts = ref<CloneAlertOut[]>([])

async function loadAlerts(): Promise<void> {
  try {
    // `.items` 不能省：列表接口返回的是信封 {items,total,limit,offset}，
    // 少了它 `alerts` 就是个对象，v-for 会遍历出 4 个字段名当告警。
    alerts.value = (await machineApi.cloneAlerts()).items
  } catch {
    // 告警拉不到不影响配对，安静降级：页面主体是配对，不是告警
    alerts.value = []
  }
}

void loadAlerts()

// --------------------------------------------------------------------------- //
// 配对（**主路径**：点某一行的「配给…」）
//
// 配对绑的是**名单条目**（一个人），不是某场比赛的选手 —— 所以对话框里两步：
// 先选名单，再选人。这两步收在 RosterPersonPicker 里，因为「改派」也要用同一个
// 东西，而"换名单要清掉已选的人"这种细节抄第二遍一定会漏。
//
// 为什么不做成页面顶部一个常驻表单（第一版就是那样）：教师到现场先看到的应该是
// "有哪几台机器等我认领"，而表单要求他**先选人、再回想是哪台机器** —— 顺序反了。
// 而且顶部那个输入框和表格里的「配对码」列长得一样，看起来像两个码。现在配对码
// 只属于**那一台机器**（显示在它那一行里、带剩余时间），教师点那一行的按钮，
// 对话框里只需选人。
// --------------------------------------------------------------------------- //

/** 待配对的机器，或已配对、要再绑一份名单的机器。 */
type BindTarget = PendingMachineOut | BoundMachineOut

const bindTarget = ref<BindTarget | null>(null)
const bindOpen = ref(false)
const bindEntryId = ref<number | null>(null)

function askBind(row: BindTarget): void {
  bindTarget.value = row
  bindEntryId.value = null
  bindOpen.value = true
}

function isPending(row: BindTarget | null): row is PendingMachineOut {
  return !!row && 'pair_code_expires_in' in row
}

// --------------------------------------------------------------------------- //
// 同名建议：配对之后，这台机器在其他名单里同名的人一键「同时绑定」
// --------------------------------------------------------------------------- //

const suggestOpen = ref(false)
const suggestAgent = ref<{ id: number; label: string } | null>(null)
const suggestions = ref<BindingSuggestionOut[]>([])

function offerSuggestions(result: BindResultOut, label: string): void {
  suggestAgent.value = { id: result.agent_id, label }
  suggestions.value = result.suggestions ?? []
  suggestOpen.value = suggestions.value.length > 0
}

async function askSuggestions(row: BoundMachineOut): Promise<void> {
  try {
    const page = await machineApi.suggestions(row.id)
    suggestAgent.value = { id: row.id, label: row.hostname || row.machine_id }
    suggestions.value = page.items
    if (!page.items.length) ElMessage.info('其他名单里没有同名且未绑定的人')
    suggestOpen.value = page.items.length > 0
  } catch (err) {
    ElMessage.error(describeError(err))
  }
}

// --------------------------------------------------------------------------- //
// 已配对的机器：每台列出全部名单绑定，可逐份解绑、再绑一份
// --------------------------------------------------------------------------- //

const bound = useList<BoundMachineOut>({
  rowKey: (row) => row.id,
  loader: ({ limit, offset, signal }) => machineApi.bound({ limit, offset }, signal),
})

async function reloadAll(): Promise<void> {
  await Promise.all([pending.reload(), bound.reload()])
}

const unbindOne = useMutation(
  (target: { agentId: number; rosterId?: number }) =>
    agentApi.unbind(target.agentId, target.rosterId),
  { onDone: reloadAll },
)

async function askUnbind(row: BoundMachineOut, binding?: AgentBindingOut): Promise<void> {
  const what = binding ? `${binding.roster_name} · ${binding.player_no}` : '全部名单'
  const last = !binding || (row.bindings ?? []).length === 1
  try {
    await ElMessageBox.confirm(
      last ? '机器回到待配对，下一轮心跳显示新的配对码。' : '其他名单的绑定不变。',
      `解除 ${row.hostname || row.machine_id} 的绑定：${what}？`,
      { type: 'warning', confirmButtonText: '解除绑定', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await unbindOne.run({ agentId: row.id, rosterId: binding?.roster_id })
}

const bindFromRow = useMutation(
  () => {
    const row = bindTarget.value
    if (!row) throw new Error('没有选中机器')
    if (!bindEntryId.value) throw new Error('请选择选手')
    // 走"按机器 id 配对"这条路：教师是点了这一行才进来的，"此刻站在这台机器前面"
    // 由这一次点击表达。要更强的证明就走工具栏的「按码配对」—— 那个必须输码。
    return machineApi.bind(row.id, bindEntryId.value)
  },
  {
    // 服务端的回执带着绑到了谁，比界面自己编一句"配对成功"有用
    success: (result) =>
      `已配对：${result.player_no}${result.roster_name ? ` · ${result.roster_name}` : ''}`,
    onDone: async (result) => {
      const row = bindTarget.value
      bindOpen.value = false
      bindTarget.value = null
      await reloadAll()
      offerSuggestions(result, row?.hostname || row?.machine_id || '')
    },
  },
)

// --------------------------------------------------------------------------- //
// 按码配对（**次要路径**：手里有码、但列表里找不到那台机器时）
// --------------------------------------------------------------------------- //

const byCodeOpen = ref(false)

// 弹窗只挂载一次：每次打开重拉名单与条目，看得到新建的名单
const bindPicker = ref<InstanceType<typeof RosterPersonPicker> | null>(null)
const codePicker = ref<InstanceType<typeof RosterPersonPicker> | null>(null)
watch(bindOpen, (open) => {
  if (open) void bindPicker.value?.reload()
})
watch(byCodeOpen, (open) => {
  if (open) void codePicker.value?.reload()
})
const pairCode = ref('')
const pairEntryId = ref<number | null>(null)

const bindByCode = useMutation(
  async () => {
    const code = pairCode.value.trim()
    if (!code) throw new Error('请输入配对码')
    if (!pairEntryId.value) throw new Error('请选择选手')
    return machineApi.bindByCode(code, pairEntryId.value)
  },
  {
    success: (result) => `已配对：${result.player_no || '—'}`,
    onDone: async (result) => {
      pairCode.value = ''
      pairEntryId.value = null
      byCodeOpen.value = false
      await reloadAll()
      offerSuggestions(result, `${result.roster_name} · ${result.player_no}`)
    },
  },
)

// --------------------------------------------------------------------------- //
// 装机设置（抽屉：统一注册密钥）
//
// 它降级成抽屉是有理由的：一间机房**一次**的动作，不该和"每天要做的配对"同等
// 地摆在页面上。第一版把两者平铺在同一页，教师的第一反应就是"这里有两套密钥体系？"
// --------------------------------------------------------------------------- //

const settingsOpen = ref(false)

// --------------------------------------------------------------------------- //
// 运行参数（心跳节奏与离线判定）
//
// 放在这一页的工具栏里：它与「装机设置」一样是"整间机房一次"的动作，而这一页
// 管的就是这类事。改完一轮心跳内生效，不用重启服务端。
// --------------------------------------------------------------------------- //

const runtimeOpen = ref(false)
const runtimeLoading = ref(false)
const runtimeError = ref<string | null>(null)
const runtime = ref<RuntimeSettingsOut | null>(null)

/** 对话框里的三个输入。打开时用服务端的当前值填。 */
const runtimeForm = ref<RuntimeSettingsOut>({
  tick_idle_seconds: 30,
  tick_active_seconds: 2,
  offline_after_seconds: 90,
})

async function loadRuntime(): Promise<void> {
  try {
    runtime.value = await settingsApi.runtime()
  } catch {
    // 区块里的当前值读不到不影响页面；打开对话框时再报错
  }
}

onMounted(() => void loadRuntime())

async function openRuntime(): Promise<void> {
  runtimeOpen.value = true
  runtimeLoading.value = true
  runtimeError.value = null
  try {
    runtime.value = await settingsApi.runtime()
    runtimeForm.value = { ...runtime.value }
  } catch (error) {
    runtimeError.value = describeError(error)
    runtime.value = null
  } finally {
    runtimeLoading.value = false
  }
}

const saveRuntime = useMutation(() => settingsApi.saveRuntime(runtimeForm.value), {
  onDone: async (result) => {
    runtime.value = result
    runtimeForm.value = { ...result }
    runtimeOpen.value = false
  },
})

// --------------------------------------------------------------------------- //
// 移除待配对的机器
// --------------------------------------------------------------------------- //

const revokeTarget = ref<PendingMachineOut | null>(null)
const revokeOpen = ref(false)

const revoke = useMutation(() => {
  const row = revokeTarget.value
  if (!row) throw new Error('没有选中机器')
  // `confirm` 是主机名。没有主机名的机器用机器码兜底 —— 总得有一个能打的东西
  return machineApi.revokePending(row.id, row.hostname || row.machine_id)
}, {
  onDone: async () => {
    revokeOpen.value = false
    revokeTarget.value = null
    await pending.reload()
  },
})

function askRevoke(row: PendingMachineOut): void {
  revokeTarget.value = row
  revokeOpen.value = true
}

const clearPending = useMutation(() => machineApi.clearPending(GLOBAL_CONFIRM), {
  onDone: async () => {
    clearPendingOpen.value = false
    await pending.reload()
  },
})

const clearPendingOpen = ref(false)

// 「召集机器」：服务端立即在局域网里喊一次自己的地址（§11）
const announcing = ref(false)
const announceState = ref<AnnounceStatusOut | null>(null)
const announceLoadError = ref<string | null>(null)

async function loadAnnounce(): Promise<void> {
  try {
    announceState.value = await machineApi.announceStatus()
    announceLoadError.value = null
  } catch (err) {
    announceLoadError.value = describeError(err)
  }
}

usePolling(loadAnnounce, { interval: 15000, immediate: true })

async function announceNow(): Promise<void> {
  announcing.value = true
  try {
    announceState.value = await machineApi.announce()
    // 后台任务每秒处理一次请求：稍等再读现状
    await new Promise((resolve) => setTimeout(resolve, 1500))
    const status = await machineApi.announceStatus()
    announceState.value = status
    if (status.last_error) ElMessage.warning(status.last_error)
    else ElMessage.success(`已召集：${status.last_url ?? ''}`)
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    announcing.value = false
  }
}

function askClearPending(): void {
  if (!pending.total.value) {
    ElMessage.info('无待配对机器')
    return
  }
  clearPendingOpen.value = true
}

function onMore(command: string): void {
  if (command === 'runtime') void openRuntime()
  else if (command === 'settings') settingsOpen.value = true
  else if (command === 'clear') askClearPending()
}

// 快捷操作入口：`?do=bind` 打开按码配对
watch(
  () => route.query.do,
  (action) => {
    if (action !== 'bind') return
    byCodeOpen.value = true
    void router.replace({ query: { ...route.query, do: undefined } })
  },
  { immediate: true },
)

// --------------------------------------------------------------------------- //
// 诊断包（这台机器**最新一份**现场）
//
// 对话框是共享组件（`DiagnosticsDialog`）：场次里的机器（`OverviewView`）也要用
// 同一份，抄两遍必然只改一处。
// --------------------------------------------------------------------------- //

const diagTarget = ref<PendingMachineOut | null>(null)
const diagOpen = ref(false)

function askDiagnostics(row: PendingMachineOut): void {
  diagTarget.value = row
  diagOpen.value = true
}

// --------------------------------------------------------------------------- //
// 统一注册密钥
// --------------------------------------------------------------------------- //

const keys = useList<BootstrapKeyOut>({
  rowKey: (row) => row.id,
  loader: ({ limit, offset, signal }) => bootstrapKeyApi.list({ limit, offset }, signal),
})

const issuedKey = ref<BootstrapKeyIssuedOut | null>(null)
const keyDialog = ref(false)
const issueLabel = ref('')
const issueDays = ref<number | null>(null)
const issueOpen = ref(false)

const issueKey = useMutation(() => bootstrapKeyApi.issue({
  label: issueLabel.value.trim() || null,
  note: null,
  expires_days: issueDays.value,
}), {
  onDone: async (result) => {
    issuedKey.value = result
    issueOpen.value = false
    issueLabel.value = ''
    issueDays.value = null
    keyDialog.value = true
    await keys.reload()
  },
})

const revokeKey = useMutation((key: BootstrapKeyOut) => bootstrapKeyApi.revoke(key.id), {
  onDone: () => keys.reload(),
})

async function askRevokeKey(key: BootstrapKeyOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `「${key.label || '#' + key.id}」吊销后不能再用于注册机器，不可恢复；已注册的机器不受影响。`,
      '吊销统一密钥？',
      { type: 'warning', confirmButtonText: '吊销', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await revokeKey.run(key)
}

/**
 * 删除密钥。
 *
 * 这里用普通确认而不是"打一遍名字"：密钥不在约定的五类结构性数据里
 * （名单/场次/选手/机器/题目），服务端也没有对它做 `confirm` 校验 ——
 * 而那几种要打名字，是因为删错了会把一批不可逆的数据一起带走。
 * 这一条只抹掉一把钥匙的记录，所以把力气花在把"吊销 vs 删除"说清楚上。
 */
const deleteKey = useMutation((key: BootstrapKeyOut) => bootstrapKeyApi.remove(key.id), {
  onDone: () => keys.reload(),
})

async function askDeleteKey(key: BootstrapKeyOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `将删除「${key.label || '#' + key.id}」的记录，不可恢复。`,
      '删除统一密钥？',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await deleteKey.run(key)
}

async function copyText(value: string | undefined, what: string): Promise<void> {
  if (!value) return
  try {
    await navigator.clipboard.writeText(value)
    ElMessage.success(`已复制${what}`)
  } catch {
    // 非 HTTPS 或浏览器策略下剪贴板不可用，提示手工复制而不是静默失败
    ElMessage.warning('复制失败，请手动复制')
  }
}

function keyState(key: BootstrapKeyOut): { label: string; type: 'success' | 'info' | 'warning' } {
  if (key.revoked_at) return { label: '已吊销', type: 'info' }
  if (key.expires_at && new Date(key.expires_at) < new Date()) {
    return { label: '已过期', type: 'warning' }
  }
  return { label: '有效', type: 'success' }
}

/** 配对码还剩多久。过期了就直说 —— 教师输一个过期码会得到 404，不如在这里看见。 */
function pairCodeLeft(row: PendingMachineOut): { expired: boolean; text: string } {
  const seconds = row.pair_code_expires_in
  if (seconds === null || seconds === undefined) return { expired: false, text: '—' }
  if (seconds <= 0) return { expired: true, text: '已过期' }
  if (seconds < 120) return { expired: false, text: `剩 ${seconds} 秒` }
  return { expired: false, text: `剩 ${Math.round(seconds / 60)} 分钟` }
}
</script>

<template>
  <PageShell
    title="机器配对"
    :error="pending.error.value"
    retryable
    @retry="pending.reload"
  >
    <template #toolbar>
      <el-button type="primary" @click="byCodeOpen = true">
        <el-icon><Key /></el-icon>按码配对
      </el-button>
      <el-button :loading="pending.loading.value" @click="pending.reload">
        <el-icon><Refresh /></el-icon>刷新
      </el-button>
      <el-dropdown trigger="click" @command="onMore">
        <el-button>
          更多<el-icon class="el-icon--right"><ArrowDown /></el-icon>
        </el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="runtime" :icon="'Timer'">运行参数</el-dropdown-item>
            <el-dropdown-item command="settings" :icon="'Setting'">装机设置</el-dropdown-item>
            <el-dropdown-item
              command="clear"
              divided
              class="danger-item"
              :disabled="!pending.total.value"
            >
              清空待配对列表
            </el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </template>

    <SectionCard title="召集机器">
      <template #extra>
        <el-button type="primary" :loading="announcing" @click="announceNow">
          <el-icon><Promotion /></el-icon>立即召集
        </el-button>
      </template>
      <el-alert
        v-if="announceLoadError"
        type="error"
        :closable="false"
        show-icon
        :title="announceLoadError"
        style="margin-bottom: 12px"
      />
      <el-alert
        v-if="announceState?.last_error"
        type="warning"
        :closable="false"
        show-icon
        :title="announceState.last_error"
        style="margin-bottom: 12px"
      />
      <el-descriptions :column="3" class="kv">
        <el-descriptions-item label="周期广播">
          <el-tag v-if="announceState?.enabled" type="success" effect="light">开启</el-tag>
          <el-tag v-else-if="announceState" type="info" effect="plain">关闭</el-tag>
          <span v-else class="muted">—</span>
        </el-descriptions-item>
        <el-descriptions-item label="广播地址">
          <span class="mono">{{ announceState?.last_url || '—' }}</span>
          <span v-if="announceState" class="muted"> · UDP {{ announceState.port }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="上次广播">
          {{ announceState?.last_sent_at ? formatTime(announceState.last_sent_at) : '从未' }}
        </el-descriptions-item>
      </el-descriptions>
    </SectionCard>

    <el-alert
      v-for="alert in alerts"
      :key="alert.fingerprint"
      :type="alert.bound_count > 0 ? 'warning' : 'info'"
      :closable="false"
      show-icon
      class="clone-alert"
    >
      <template #title>
        <template v-if="alert.bound_count > 0">
          疑似克隆镜像：{{ alert.machine_count }} 台机器共用同一硬件指纹，其中
          {{ alert.bound_count }} 台已配对
        </template>
        <template v-else>
          {{ alert.machine_count }} 台机器共用同一硬件指纹（均未配对）
        </template>
      </template>
      <template #default>
        <span class="mono">{{ alert.fingerprint }}</span>
      </template>
    </el-alert>

    <SectionCard title="待配对的机器" :meta="`${pending.total.value} 台`" flush>
      <DataTable
        :rows="pending.rows.value"
        :row-key="(row: PendingMachineOut) => row.id"
        :loading="pending.loading.value"
        :total="pending.total.value"
        :page="pending.page.value"
        :page-size="pending.pageSize.value"
        empty-text="无待配对机器"
        @update:page="pending.setPage"
        @update:pageSize="pending.setPageSize"
      >
        <template #columns>
          <el-table-column label="主机名" width="150">
            <template #default="{ row }">
              <span>{{ row.hostname || '—' }}</span>
            </template>
          </el-table-column>

          <el-table-column label="机器码" width="170">
            <template #default="{ row }">
              <el-tooltip :content="row.machine_id">
                <span class="mono muted">{{ row.machine_id.slice(0, 18) }}</span>
              </el-tooltip>
            </template>
          </el-table-column>

          <el-table-column label="最后心跳" width="160">
            <template #default="{ row }">
              <div>{{ formatSince(row.seconds_since_seen, '从未') }}</div>
              <div class="cell-sub">{{ formatTime(row.last_seen_at) }}</div>
            </template>
          </el-table-column>

          <el-table-column label="配对码" width="120">
            <template #default="{ row }">
              <el-tag v-if="pairCodeLeft(row).expired" type="danger" effect="plain">已过期</el-tag>
              <span v-else class="cell-sub">{{ pairCodeLeft(row).text }}</span>
            </template>
          </el-table-column>

          <el-table-column label="指纹" width="110">
            <template #default="{ row }">
              <el-tag v-if="row.fingerprint_peers > 1" type="warning" effect="plain">
                {{ row.fingerprint_peers }} 台共用
              </el-tag>
              <span v-else class="muted">唯一</span>
            </template>
          </el-table-column>

          <el-table-column label="Agent 版本" min-width="140">
            <template #default="{ row }">
              <span class="cell-sub">{{ row.agent_version || '未知' }}</span>
            </template>
          </el-table-column>

          <el-table-column label="操作" width="200" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="askBind(row)">配对</el-button>
              <el-button link type="primary" size="small" @click="askDiagnostics(row)">
                诊断
              </el-button>
              <el-button link type="danger" size="small" @click="askRevoke(row)">移除</el-button>
            </template>
          </el-table-column>
        </template>

        <template #empty>
          <el-empty description="无待配对机器" :image-size="64">
            <el-button @click="settingsOpen = true">装机设置</el-button>
          </el-empty>
        </template>
      </DataTable>
    </SectionCard>

    <SectionCard title="已配对的机器" :meta="`${bound.total.value ?? 0} 台`" flush>
      <DataTable
        :rows="bound.rows.value"
        :row-key="(row: BoundMachineOut) => row.id"
        :loading="bound.loading.value"
        :total="bound.total.value"
        :page="bound.page.value"
        :page-size="bound.pageSize.value"
        empty-text="无已配对机器"
        @update:page="bound.setPage"
        @update:pageSize="bound.setPageSize"
      >
        <template #columns>
          <el-table-column label="主机名" width="170">
            <template #default="{ row }">
              <div>{{ row.hostname || '—' }}</div>
              <div class="cell-sub mono">{{ row.machine_id.slice(0, 18) }}</div>
            </template>
          </el-table-column>
          <el-table-column label="绑定（名单 · 考号 · 姓名）" min-width="320">
            <template #default="{ row }">
              <div class="binding-tags">
                <el-tag
                  v-for="item in row.bindings"
                  :key="item.roster_id"
                  closable
                  effect="plain"
                  @close="askUnbind(row, item)"
                >
                  {{ item.roster_name }} · {{ item.player_no }}{{ item.name ? ` · ${item.name}` : '' }}
                </el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="最后心跳" width="160">
            <template #default="{ row }">
              <span class="cell-sub">{{ formatTime(row.last_seen_at) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="250" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="askBind(row)">再绑一份名单</el-button>
              <el-button link type="primary" size="small" @click="askSuggestions(row)">同名建议</el-button>
              <el-button link type="danger" size="small" @click="askUnbind(row)">全部解绑</el-button>
            </template>
          </el-table-column>
        </template>
      </DataTable>
    </SectionCard>

    <SectionCard title="机房设置">
      <div class="setting-grid">
        <div class="setting-item">
          <div class="setting-head">
            <span class="setting-name">装机设置</span>
            <el-button @click="settingsOpen = true">
              <el-icon><Setting /></el-icon>管理统一密钥
            </el-button>
          </div>
          <div class="cell-sub">统一注册密钥 {{ keys.total.value }} 把</div>
        </div>
        <div class="setting-item">
          <div class="setting-head">
            <span class="setting-name">运行参数</span>
            <el-button @click="openRuntime">
              <el-icon><Timer /></el-icon>修改运行参数
            </el-button>
          </div>
          <div v-if="runtime" class="cell-sub">
            空闲心跳 {{ runtime.tick_idle_seconds }} 秒 · 有活心跳
            {{ runtime.tick_active_seconds }} 秒 · 离线判定 {{ runtime.offline_after_seconds }} 秒
          </div>
          <div v-else class="cell-sub">—</div>
        </div>
      </div>
    </SectionCard>

    <el-drawer v-model="settingsOpen" title="装机设置：统一注册密钥" size="880px">
      <el-button type="primary" style="margin-bottom: 12px" @click="issueOpen = true">
        <el-icon><Plus /></el-icon>签发统一密钥
      </el-button>

      <DataTable
        :rows="keys.rows.value"
        :row-key="(row: BootstrapKeyOut) => row.id"
        :loading="keys.loading.value"
        :total="keys.total.value"
        :page="keys.page.value"
        :page-size="keys.pageSize.value"
        empty-text="无统一密钥"
      >
        <template #columns>
          <el-table-column label="用途" min-width="160">
            <template #default="{ row }">{{ row.label || '—' }}</template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="keyState(row).type" effect="plain">{{ keyState(row).label }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="使用次数" width="90" align="right">
            <template #default="{ row }">{{ row.use_count }}</template>
          </el-table-column>
          <el-table-column label="最后使用" width="160">
            <template #default="{ row }">
              <span class="cell-sub">{{ formatTime(row.last_used_at) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="签发时间" width="160">
            <template #default="{ row }">
              <span class="cell-sub">{{ formatTime(row.created_at) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="有效期至" width="160">
            <template #default="{ row }">
              <span class="cell-sub">{{ row.expires_at ? formatTime(row.expires_at) : '长期' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="备注" min-width="140">
            <template #default="{ row }">
              <span class="cell-sub">{{ row.note || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="120" fixed="right">
            <template #default="{ row }">
              <el-button
                link
                type="danger"
                size="small"
                :disabled="!!row.revoked_at"
                @click="askRevokeKey(row)"
              >
                吊销
              </el-button>
              <el-button
                link
                type="danger"
                size="small"
                :disabled="!!row.use_count"
                @click="askDeleteKey(row)"
              >
                删除
              </el-button>
            </template>
          </el-table-column>
        </template>

        <template #empty>
          <el-empty description="无统一密钥" :image-size="64">
            <el-button type="primary" @click="issueOpen = true">签发统一密钥</el-button>
          </el-empty>
        </template>
      </DataTable>
    </el-drawer>

    <FormDialog
      v-model="runtimeOpen"
      title="运行参数"
      :submitting="saveRuntime.pending.value"
      confirm-text="保存"
      @submit="saveRuntime.run(undefined)"
    >
      <el-alert
        v-if="runtimeError"
        type="error"
        :closable="false"
        show-icon
        :title="runtimeError"
        style="margin-bottom: 12px"
      />
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="对全部机器生效"
        style="margin-bottom: 12px"
      />

      <el-form v-loading="runtimeLoading" label-width="140px">
        <el-form-item label="空闲心跳 / 扫描">
          <el-input-number
            v-model="runtimeForm.tick_idle_seconds"
            :min="5"
            :max="3600"
            :controls="false"
            style="width: 110px"
          />
          <span class="unit">秒</span>
        </el-form-item>
        <el-form-item label="有活心跳">
          <el-input-number
            v-model="runtimeForm.tick_active_seconds"
            :min="1"
            :max="60"
            :controls="false"
            style="width: 110px"
          />
          <span class="unit">秒</span>
        </el-form-item>
        <el-form-item label="离线判定">
          <el-input-number
            v-model="runtimeForm.offline_after_seconds"
            :min="10"
            :max="86400"
            :controls="false"
            style="width: 110px"
          />
          <span class="unit">秒</span>
        </el-form-item>
      </el-form>

      <p v-if="runtime" class="cell-sub" style="margin: 0">
        当前：空闲 {{ runtime.tick_idle_seconds }} 秒 · 有活
        {{ runtime.tick_active_seconds }} 秒 · 离线 {{ runtime.offline_after_seconds }} 秒
      </p>
    </FormDialog>

    <ConfirmByNameDialog
      v-model="revokeOpen"
      title="移除机器？"
      :expected="revokeTarget?.hostname || revokeTarget?.machine_id || ''"
      :submitting="revoke.pending.value"
      confirm-text="移除"
      detail="将移除这台机器并作废其凭据，不可恢复；机器下次开机会用统一密钥重新注册。"
      @confirm="revoke.run(undefined)"
    />

    <ConfirmByNameDialog
      v-model="clearPendingOpen"
      title="清空待配对列表？"
      :expected="GLOBAL_CONFIRM"
      :submitting="clearPending.pending.value"
      confirm-text="清空"
      :detail="`将移除 ${pending.total.value} 台待配对机器并作废其凭据，不可恢复；机器下次开机会用统一密钥重新注册。`"
      @confirm="clearPending.run(undefined)"
    />

    <FormDialog
      v-model="issueOpen"
      title="签发统一密钥"
      :submitting="issueKey.pending.value"
      confirm-text="签发"
      @submit="issueKey.run(undefined)"
    >
      <el-form label-width="90px">
        <el-form-item label="用途">
          <el-input v-model="issueLabel" placeholder="例如 2025 机房镜像" maxlength="200" />
        </el-form-item>
        <el-form-item label="有效天数">
          <el-input-number
            v-model="issueDays"
            :min="1"
            :max="3650"
            :controls="false"
            placeholder="留空为长期"
          />
          <span class="unit">天</span>
        </el-form-item>
      </el-form>
    </FormDialog>

    <el-dialog v-model="keyDialog" title="统一密钥" width="640px">
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="明文只显示这一次"
        style="margin-bottom: 12px"
      />

      <div class="key-box mono">{{ issuedKey?.key }}</div>

      <div class="cell-sub" style="margin-top: 12px">
        装机命令
        <pre class="cmd">python3 install.py --bundle ./syncoj-agent-x.tar.gz \
    --server https://10.0.0.1:8443 \
    --bootstrap-key-file /etc/syncoj/bootstrap.key</pre>
      </div>

      <template #footer>
        <el-button @click="copyText(issuedKey?.key, '密钥')">
          <el-icon><CopyDocument /></el-icon>复制密钥
        </el-button>
        <el-button type="primary" @click="keyDialog = false">完成</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="bindOpen" title="配对机器" width="580px">
      <p class="cell-sub" style="margin-top: 0">
        机器 <span class="mono">{{ bindTarget?.hostname || bindTarget?.machine_id || '—' }}</span>
        <template v-if="isPending(bindTarget)"> · 配对码{{ pairCodeLeft(bindTarget).text }}</template>
        <template v-else-if="bindTarget">
          · 已绑：{{ (bindTarget.bindings ?? []).map((b) => `${b.roster_name} · ${b.player_no}`).join('，') }}
        </template>
      </p>
      <RosterPersonPicker
        ref="bindPicker"
        v-model="bindEntryId"
        :default-roster-id="contest.current?.default_roster_id ?? null"
      />
      <template #footer>
        <el-button @click="bindOpen = false">取消</el-button>
        <el-button
          type="primary"
          :disabled="!bindEntryId"
          :loading="bindFromRow.pending.value"
          @click="bindFromRow.run(undefined)"
        >
          配对
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="byCodeOpen" title="按码配对" width="580px">
      <div class="pair-row">
        <span class="field-label">配对码</span>
        <el-input
          v-model="pairCode"
          placeholder="例如 382915"
          style="width: 230px"
          class="mono-input"
          maxlength="6"
          @keyup.enter="bindByCode.run(undefined)"
        />
      </div>
      <RosterPersonPicker
        ref="codePicker"
        v-model="pairEntryId"
        :default-roster-id="contest.current?.default_roster_id ?? null"
      />
      <template #footer>
        <el-button @click="byCodeOpen = false">取消</el-button>
        <el-button
          type="primary"
          :disabled="!pairCode.trim() || !pairEntryId"
          :loading="bindByCode.pending.value"
          @click="bindByCode.run(undefined)"
        >
          配对
        </el-button>
      </template>
    </el-dialog>

    <BindingSuggestionsDialog
      v-model="suggestOpen"
      :agent-id="suggestAgent?.id ?? null"
      :label="suggestAgent?.label"
      :suggestions="suggestions"
      @done="reloadAll"
    />

    <DiagnosticsDialog
      v-model="diagOpen"
      :agent-id="diagTarget?.id ?? 0"
      :label="diagTarget?.hostname || diagTarget?.machine_id || ''"
    />
  </PageShell>
</template>

<style scoped>
.pair-row {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  margin-bottom: 12px;
}

.field-label {
  font-size: 13px;
  color: var(--sj-text-2);
}

.mono-input :deep(input) {
  font-family: var(--sj-mono);
  letter-spacing: 2px;
}

.unit {
  margin-left: 8px;
  color: var(--sj-text-3);
}

.key-box {
  padding: 14px;
  border: 1px dashed var(--sj-warning);
  border-radius: var(--sj-radius-sm);
  background: var(--sj-warning-soft);
  color: var(--sj-text);
  font-size: 16px;
  word-break: break-all;
  user-select: all;
}

.cmd {
  margin: 8px 0 0;
  padding: 10px;
  color: var(--sj-text);
  background: var(--sj-surface-2);
  border: 1px solid var(--sj-border);
  border-radius: var(--sj-radius-sm);
  font-size: 12px;
  white-space: pre-wrap;
}

.clone-alert {
  margin-top: 16px;
}

.clone-alert + .section-card,
.section-card + .clone-alert {
  margin-top: 16px;
}

.kv :deep(.el-descriptions__body) {
  background: transparent;
}

.setting-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 16px;
}

.setting-item {
  padding: 14px 16px;
  border: 1px solid var(--sj-border);
  border-radius: var(--sj-radius-sm);
  background: var(--sj-surface-2);
}

.setting-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 6px;
}

.setting-name {
  font-weight: 600;
}

.muted {
  color: var(--sj-text-3);
}

.danger-item {
  color: var(--el-color-danger);
}

.binding-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
</style>

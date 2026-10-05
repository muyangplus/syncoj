<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { bootstrapKeyApi, machineApi } from '@/api'
import { GLOBAL_CONFIRM } from '@/api/endpoints'
import type {
  BootstrapKeyIssuedOut,
  BootstrapKeyOut,
  CloneAlertOut,
  PendingMachineOut,
} from '@/api/types'
import ConfirmByNameDialog from '@/components/ConfirmByNameDialog.vue'
import DataTable from '@/components/DataTable.vue'
import FormDialog from '@/components/FormDialog.vue'
import HelpTip from '@/components/HelpTip.vue'
import PageShell from '@/components/PageShell.vue'
import RosterPersonPicker from '@/components/RosterPersonPicker.vue'
import { useList } from '@/composables/useList'
import { useMutation } from '@/composables/useMutation'
import { useContestStore } from '@/stores/contest'
import { formatSince, formatTime } from '@/utils/format'

const contest = useContestStore()

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
    alerts.value = await machineApi.cloneAlerts()
  } catch {
    // 告警拉不到不影响配对，安静降级：页面主体是配对，不是告警
    alerts.value = []
  }
}

void loadAlerts()

// --------------------------------------------------------------------------- //
// 配给谁
//
// 配对绑的是**名单条目**（一个人），不是某场比赛的选手 —— 所以必须两步：
// 先选名单，再选人。这两步收在 RosterPersonPicker 里，因为「改派」也要用
// 同一个东西，而"换名单要清掉已选的人"这种细节抄第二遍一定会漏。
// --------------------------------------------------------------------------- //

const entryId = ref<number | null>(null)

// --------------------------------------------------------------------------- //
// 配对
// --------------------------------------------------------------------------- //

const pairCode = ref('')

const bindByCode = useMutation(
  async () => {
    const code = pairCode.value.trim()
    if (!code) throw new Error('请输入机器上显示的配对码')
    if (!entryId.value) throw new Error('请先选择这份名单里的一个人')
    return machineApi.bindByCode(code, entryId.value)
  },
  {
    // 服务端的回执里带着"绑到了谁、在哪份名单"，比界面自己编一句"配对成功"有用
    onDone: async () => {
      pairCode.value = ''
      await pending.reload()
    },
  },
)

const bindFromList = useMutation(
  async (row: PendingMachineOut) => {
    if (!entryId.value) throw new Error('先在下面选一位选手，再点「配对」')
    // 手工点选也把配对码带上（如果填了）：主机名可能是重复的，而配对码是唯一
    // 能证明"教师确实站在这台机器前面"的东西
    return machineApi.bind(row.id, entryId.value, pairCode.value.trim() || undefined)
  },
  { onDone: () => pending.reload() },
)

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

function askClearPending(): void {
  if (!pending.total.value) {
    ElMessage.info('待配对列表本来就是空的')
    return
  }
  clearPendingOpen.value = true
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
      `吊销「${key.label || '#' + key.id}」？\n\n` +
        '已经注册好的机器不受影响（它们手里是各自的 token，不是这把密钥），' +
        '但之后没人能再用它注册。',
      '吊销统一密钥',
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
      `彻底删掉「${key.label || '#' + key.id}」？\n\n` +
        '「吊销」与「删除」不是一回事：吊销会**留痕**（用过几次、最后什么时候用的都还在），' +
        '密钥疑似泄漏时该用吊销；删除是抹掉记录，只在「签错了、一次都没用过」时才合适。\n' +
        '用过的密钥服务端会直接拒绝删除。',
      '删除统一密钥',
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
    ElMessage.warning('浏览器不允许自动复制，请手工选中复制')
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
  if (seconds < 120) return { expired: false, text: `还有 ${seconds} 秒` }
  return { expired: false, text: `还有 ${Math.round(seconds / 60)} 分钟` }
}
</script>

<template>
  <PageShell
    title="机器配对"
    hint="读机器桌面上的六位配对码，认领到名单里的某个人。"
    :error="pending.error.value"
    error-action="待配对列表取不到时，先别急着发密钥。"
    retryable
    @retry="pending.reload"
  >
    <template #hint>
      <HelpTip>
        用镜像统一密钥注册上来的机器还没有归属 —— 服务端不知道它是哪个学生。
        配对是永久的，绑的是名单里的<strong>人</strong>，换场次不用重配。
      </HelpTip>
    </template>
    <template #toolbar>
      <el-button size="small" :loading="pending.loading.value" @click="pending.reload">
        刷新
      </el-button>
      <el-button size="small" @click="issueOpen = true">签发统一密钥</el-button>
    </template>

    <!-- 克隆镜像告警：多台机器共用同一个硬件指纹，是"镜像在跑过之后才克隆"的信号 -->
    <el-alert
      v-for="alert in alerts"
      :key="alert.fingerprint"
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom: 12px"
    >
        <template #title>疑似克隆镜像：{{ alert.machine_count }} 台机器共用同一个硬件指纹</template>
        <template #default>
          {{ alert.fingerprint }}<br />
          撞指纹说明镜像是在某台机器跑过之后才克隆的，那批机器里可能混进了别人的凭据。
          <strong>建议逐台确认配对，别用「从列表里配对」批量点。</strong>
        </template>
      </el-alert>

    <el-card shadow="never" style="margin-bottom: 12px">
      <div class="pair-row">
        <span class="field-label">配对码</span>
        <el-input
          v-model="pairCode"
          placeholder="机器桌面上「配对码.txt」里的 6 位数字"
          style="width: 230px"
          class="mono-input"
          maxlength="6"
          @keyup.enter="bindByCode.run(undefined)"
        />

        <span class="field-label">配给</span>
        <RosterPersonPicker
          v-model="entryId"
          :default-roster-id="contest.current?.default_roster_id ?? null"
        />

        <el-button
          type="primary"
          :loading="bindByCode.pending.value"
          :disabled="!pairCode.trim() || !entryId"
          @click="bindByCode.run(undefined)"
        >
          配对
        </el-button>
      </div>

      <p class="page-hint">还没选人 —— 先在「配给」那一栏选名单，再选这份名单里的一个人。</p>
    </el-card>

    <div class="list-head">
      <h3 class="section-title">
        待配对的机器
        <span class="muted">（{{ pending.total.value }} 台）</span>
      </h3>
      <div class="toolbar">
        <el-button
          size="small"
          type="danger"
          plain
          :disabled="!pending.total.value"
          :loading="clearPending.pending.value"
          @click="askClearPending"
        >
          清空列表
        </el-button>
      </div>
    </div>

    <DataTable
      :rows="pending.rows.value"
      :row-key="(row: PendingMachineOut) => row.id"
      :loading="pending.loading.value"
      :total="pending.total.value"
      :page="pending.page.value"
      :page-size="pending.pageSize.value"
      empty-text="没有待配对的机器"
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

        <el-table-column label="最后心跳" width="150">
          <template #default="{ row }">
            <div>{{ formatSince(row.seconds_since_seen, '从未') }}</div>
            <div class="cell-sub">{{ formatTime(row.last_seen_at) }}</div>
          </template>
        </el-table-column>

        <el-table-column label="配对码" width="120">
          <template #default="{ row }">
            <el-tag v-if="pairCodeLeft(row).expired" type="danger" size="small" effect="plain">
              已过期
            </el-tag>
            <span v-else class="cell-sub">{{ pairCodeLeft(row).text }}</span>
          </template>
        </el-table-column>

        <el-table-column label="指纹" width="110">
          <template #default="{ row }">
            <el-tooltip
              v-if="row.fingerprint_peers > 1"
              :content="`同一个硬件指纹上还有 ${row.fingerprint_peers - 1} 台机器 —— 疑似克隆镜像`"
            >
              <el-tag type="warning" size="small" effect="plain">
                {{ row.fingerprint_peers }} 台共用
              </el-tag>
            </el-tooltip>
            <span v-else class="muted">唯一</span>
          </template>
        </el-table-column>

        <el-table-column label="客户端" min-width="140">
          <template #default="{ row }">
            <span class="cell-sub">版本 {{ row.agent_version || '未知' }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button
              link
              type="primary"
              size="small"
              :loading="bindFromList.pending.value"
              :disabled="!entryId"
              @click="bindFromList.run(row)"
            >
              配对
            </el-button>
            <el-button link type="danger" size="small" @click="askRevoke(row)">移除</el-button>
          </template>
        </el-table-column>
      </template>

      <template #empty>
        <p>没有待配对的机器。</p>
        <p>用统一密钥装好的机器开机后会自动出现在这里。一台都没出现，就先确认镜像里放了 <code>bootstrap.key</code>。</p>
        <el-button type="primary" size="small" style="margin-top: 12px" @click="issueOpen = true">
          签发统一密钥
        </el-button>
      </template>
    </DataTable>

    <h3 class="section-title">统一注册密钥</h3>
    <p class="page-hint">密钥在机器上只是 <code>root</code> 只读的一份文件，<strong>不写进 <code>agent.ini</code></strong>（那个文件选手账号可读）。</p>

    <DataTable
      :rows="keys.rows.value"
      :row-key="(row: BootstrapKeyOut) => row.id"
      :loading="keys.loading.value"
      :total="keys.total.value"
      :page="keys.page.value"
      :page-size="keys.pageSize.value"
      empty-text="还没有统一密钥"
    >
      <template #columns>
        <el-table-column label="用途" min-width="160">
          <template #default="{ row }">{{ row.label || '—' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="keyState(row).type" size="small" effect="plain">
              {{ keyState(row).label }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="用过" width="80" align="right">
          <template #default="{ row }">{{ row.use_count }}</template>
        </el-table-column>
        <el-table-column label="最后使用" width="150">
          <template #default="{ row }">
            <span class="cell-sub">{{ formatTime(row.last_used_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="签发时间" width="150">
          <template #default="{ row }">
            <span class="cell-sub">{{ formatTime(row.created_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="有效期" width="110">
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
            <!-- 删除只对"签错了、一次都没用过"才有意义：用过的钥匙不能因为
                 记录被删就当它没存在过，服务端也会拒 -->
            <el-button
              link
              type="info"
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
        <p>还没有统一密钥。</p>
        <p>装整间机房就签发一把，装机时用 <code>--bootstrap-key</code> 传给安装器。</p>
        <el-button type="primary" size="small" @click="issueOpen = true">签发一把</el-button>
      </template>
    </DataTable>

    <!-- 移除待配对机器：机器是结构性数据，按约定要打一遍主机名 -->
    <ConfirmByNameDialog
      v-model="revokeOpen"
      title="移除待配对的机器"
      :expected="revokeTarget?.hostname || revokeTarget?.machine_id || ''"
      :submitting="revoke.pending.value"
      confirm-text="移除"
      :detail="
        '这台机器会从待配对列表里消失，凭据被作废。它下次心跳会拿到 401，' +
        '然后等下次开机由注册单元重新注册一遍（会拿到新的配对码）。' +
        '要挡住它反复回来，得先吊销统一密钥。'
      "
      @confirm="revoke.run(undefined)"
    />

    <!-- 清空待配对机器：范围是"全部"，所以打的是固定字面量 all -->
    <ConfirmByNameDialog
      v-model="clearPendingOpen"
      title="清空待配对列表"
      :expected="GLOBAL_CONFIRM"
      :submitting="clearPending.pending.value"
      confirm-text="清空"
      :detail="
        `会把当前 ${pending.total.value} 台待配对机器全部移除（凭据作废）。` +
        '它们下次开机用镜像里的统一密钥还会重新注册上来 —— ' +
        '要彻底挡住，请先在上面把统一密钥吊销。'
      "
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
        <el-form-item label="用途备注">
          <el-input v-model="issueLabel" placeholder="比如「2025 机房镜像」" maxlength="200" />
        </el-form-item>
        <el-form-item label="有效天数">
          <el-input-number v-model="issueDays" :min="1" :max="3650" :controls="false" />
          <span class="page-hint" style="margin-left: 8px">留空表示长期有效</span>
        </el-form-item>
      </el-form>
      <el-alert type="warning" :closable="false" show-icon title="明文只会显示这一次">
        库里只存哈希。丢了就重新签发一把，然后把旧的吊销。
      </el-alert>
    </FormDialog>

    <el-dialog v-model="keyDialog" title="统一密钥（只显示这一次）" width="640px">
      <el-alert type="warning" :closable="false" show-icon style="margin-bottom: 12px">
        <template #title>现在抄下来 —— 关掉这个窗口就再也看不到明文了</template>
        <template #default>库里只存哈希。丢了就重新签发一把，然后把旧的吊销。</template>
      </el-alert>

      <div class="key-box mono">{{ issuedKey?.key }}</div>

      <div class="page-hint" style="margin-top: 12px">
        装机时这样用（它会被写成 <code>/etc/syncoj/bootstrap.key</code>，0600、属主 root）：
        <pre class="cmd">python3 install.py --bundle ./syncoj-agent-x.tar.gz \
    --server https://10.0.0.1:8443 \
    --bootstrap-key-file /etc/syncoj/bootstrap.key</pre>
      </div>

      <template #footer>
        <el-button @click="copyText(issuedKey?.key, '密钥')">复制</el-button>
        <el-button type="primary" @click="keyDialog = false">我已抄好</el-button>
      </template>
    </el-dialog>
  </PageShell>
</template>

<style scoped>
.pair-row {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
}

.field-label {
  font-size: 13px;
  color: #606266;
}

.mono-input :deep(input) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  letter-spacing: 2px;
}

.key-box {
  padding: 14px;
  border: 1px dashed #e6a23c;
  border-radius: 4px;
  background: #fdf6ec;
  font-size: 16px;
  word-break: break-all;
  user-select: all;
}

.cmd {
  margin: 8px 0 0;
  padding: 10px;
  background: #f5f7fa;
  border-radius: 4px;
  font-size: 12px;
  white-space: pre-wrap;
}

.section-title {
  font-size: 15px;
  margin: 22px 0 10px;
}

.list-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.list-head .section-title {
  margin-bottom: 10px;
}

.muted {
  font-size: 12px;
  font-weight: 400;
  color: #909399;
}
</style>

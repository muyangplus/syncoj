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

const bindTarget = ref<PendingMachineOut | null>(null)
const bindOpen = ref(false)
const bindEntryId = ref<number | null>(null)

function askBind(row: PendingMachineOut): void {
  bindTarget.value = row
  bindEntryId.value = null
  bindOpen.value = true
}

const bindFromRow = useMutation(
  () => {
    const row = bindTarget.value
    if (!row) throw new Error('没有选中机器')
    if (!bindEntryId.value) throw new Error('请先选一个人')
    // 走"按机器 id 配对"这条路：教师是点了这一行才进来的，"此刻站在这台机器前面"
    // 由这一次点击表达。要更强的证明就走工具栏的「按码配对」—— 那个必须输码。
    return machineApi.bind(row.id, bindEntryId.value)
  },
  {
    // 服务端的回执带着绑到了谁，比界面自己编一句"配对成功"有用
    success: (result) =>
      `已把 ${result.player_no} 配给 ${result.roster_name || '名单里的人'}`,
    onDone: async () => {
      bindOpen.value = false
      bindTarget.value = null
      await pending.reload()
    },
  },
)

// --------------------------------------------------------------------------- //
// 按码配对（**次要路径**：手里有码、但列表里找不到那台机器时）
// --------------------------------------------------------------------------- //

const byCodeOpen = ref(false)
const pairCode = ref('')
const pairEntryId = ref<number | null>(null)

const bindByCode = useMutation(
  async () => {
    const code = pairCode.value.trim()
    if (!code) throw new Error('请输入机器上显示的配对码')
    if (!pairEntryId.value) throw new Error('请先选一个人')
    return machineApi.bindByCode(code, pairEntryId.value)
  },
  {
    success: (result) => `已把机器配给 ${result.player_no || '名单里的人'}`,
    onDone: async () => {
      pairCode.value = ''
      pairEntryId.value = null
      byCodeOpen.value = false
      await pending.reload()
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
    :error="pending.error.value"
    error-action="先重试；还不行就先别动密钥。"
    retryable
    @retry="pending.reload"
  >
    <template #toolbar>
      <el-button size="small" :loading="pending.loading.value" @click="pending.reload">
        刷新
      </el-button>
      <!-- 手里有码、但列表里找不到那台机器时的入口（次要路径，所以是普通按钮） -->
      <el-button size="small" @click="byCodeOpen = true">按码配对</el-button>
      <el-button size="small" @click="settingsOpen = true">装机设置</el-button>
    </template>

    <!--
      克隆镜像告警：多台机器共用同一个硬件指纹，是"镜像在跑过之后才克隆"的信号。

      但严重程度取决于**有没有人已经被绑上去**：一份镜像装遍整间机房时撞指纹是必然
      也是正常的，那时一台都还没配对，没有任何人的身份可被冒领 —— 对着教师喊"可能
      混进了别人的凭据"是错的。真的有人挂在这个指纹上时才是警告，因为快照还原那条
      "按指纹认回原机器"会认错机器，而错的那一头就是某个学生的成绩。
    -->
    <el-alert
      v-for="alert in alerts"
      :key="alert.fingerprint"
      :type="alert.bound_count > 0 ? 'warning' : 'info'"
      :closable="false"
      show-icon
      style="margin-bottom: 12px"
    >
      <template #title>
        <template v-if="alert.bound_count > 0">
          疑似克隆镜像：{{ alert.machine_count }} 台机器共用同一个硬件指纹，
          其中 {{ alert.bound_count }} 台已经配给了人
        </template>
        <template v-else>
          {{ alert.machine_count }} 台机器共用同一个硬件指纹（都还没配对）
        </template>
      </template>
      <template #default>
        <span class="mono">{{ alert.fingerprint }}</span><br />
      </template>
    </el-alert>

    <!--
      这里只留一句说明，不再放常驻表单：教师到现场先要看的是"有哪几台机器等我认领"，
      而表单要求他先选人、再回想是哪台机器 —— 顺序反了。配对动作在表格每一行里。
    -->
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

        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="askBind(row)">
              配给…
            </el-button>
            <el-button link type="danger" size="small" @click="askRevoke(row)">移除</el-button>
          </template>
        </el-table-column>
      </template>

      <template #empty>
        <p>现在没有机器等认领。</p>
        <!--
          空状态必须给出"两种可能"，而不是只写"机器开机后会自动出现在这里"——
          那句在"镜像里忘了放密钥"时是误导：教师会一直等，而机器永远不会出现。
        -->
        <p class="cell-sub">
          一直没出现，查两件事：镜像里有没有统一密钥、它连不连得上服务端。
        </p>
        <el-button type="primary" size="small" style="margin-top: 12px" @click="settingsOpen = true">
          去装机设置
        </el-button>
      </template>
    </DataTable>

    <!--
      ② 装机设置：统一注册密钥收进抽屉。
      一间机房**一次**的动作，不该和"每天要做的配对"同等地摆在页面上 —— 第一版把
      两者平铺在同一页，教师的第一反应就是"这里有两套密钥体系？"。
    -->
    <el-drawer v-model="settingsOpen" title="装机设置：统一注册密钥" size="880px">
      <el-button type="primary" size="small" style="margin-bottom: 12px" @click="issueOpen = true">
        签发一把
      </el-button>

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
        <p>装机时用 <code>--bootstrap-key</code> 传给安装器。</p>
        <el-button type="primary" size="small" @click="issueOpen = true">签发一把</el-button>
      </template>
      </DataTable>
    </el-drawer>

    <!-- 移除待配对机器：机器是结构性数据，按约定要打一遍主机名 -->
    <ConfirmByNameDialog
      v-model="revokeOpen"
      title="移除待配对的机器"
      :expected="revokeTarget?.hostname || revokeTarget?.machine_id || ''"
      :submitting="revoke.pending.value"
      confirm-text="移除"
      :detail="
        '这台机器会从待配对列表里消失、凭据作废；下次开机它还会用镜像里的统一密钥重新注册上来，' +
        '要挡住得先吊销那把密钥。'
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
        `会把当前 ${pending.total.value} 台待配对机器全部移除、凭据作废；` +
        '它们下次开机还会用镜像里的统一密钥重新注册上来。'
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
          <span class="cell-sub" style="margin-left: 8px">留空表示长期有效</span>
        </el-form-item>
      </el-form>
      <el-alert type="warning" :closable="false" show-icon title="明文只会显示这一次">
        丢了就重新签发一把，然后把旧的吊销。
      </el-alert>
    </FormDialog>

    <el-dialog v-model="keyDialog" title="统一密钥（只显示这一次）" width="640px">
      <el-alert type="warning" :closable="false" show-icon style="margin-bottom: 12px">
        <template #title>现在抄下来 —— 关掉这个窗口就再也看不到明文了</template>
        <template #default>丢了就重新签发一把，然后把旧的吊销。</template>
      </el-alert>

      <div class="key-box mono">{{ issuedKey?.key }}</div>

      <div class="cell-sub" style="margin-top: 12px">
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
    <!--
      **主路径**：从某台机器那一行点进来。这里只要选人 —— "教师此刻站在这台机器前面"
      已经由"点了那一行"表达；要更强的证明就走工具栏的「按码配对」（那个必须输码）。
    -->
    <el-dialog v-model="bindOpen" title="把机器配给谁" width="580px">
      <p class="cell-sub" style="margin-top: 0">
        机器：<span class="mono">{{ bindTarget?.hostname || bindTarget?.machine_id || '—' }}</span>
        <template v-if="bindTarget">
          · 配对码{{ pairCodeLeft(bindTarget).text }}
        </template>
      </p>
      <RosterPersonPicker
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

    <!-- **次要路径**：手里有码、但列表里找不到那台机器时（比如机器换过名字） -->
    <el-dialog v-model="byCodeOpen" title="按配对码配对" width="580px">
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
      </div>
      <RosterPersonPicker
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

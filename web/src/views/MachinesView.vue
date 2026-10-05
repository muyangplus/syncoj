<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { bootstrapKeyApi, machineApi, playerApi } from '@/api'
import type {
  BootstrapKeyIssuedOut,
  BootstrapKeyOut,
  CloneAlertOut,
  PendingMachineOut,
  PlayerOut,
} from '@/api/types'
import { useContestStore } from '@/stores/contest'
import { formatSince, formatTime } from '@/utils/format'

const contest = useContestStore()

const pending = ref<PendingMachineOut[]>([])
const alerts = ref<CloneAlertOut[]>([])
const keys = ref<BootstrapKeyOut[]>([])
const players = ref<PlayerOut[]>([])
const loading = ref(false)

async function load(): Promise<void> {
  loading.value = true
  try {
    const tasks: [Promise<PendingMachineOut[]>, Promise<CloneAlertOut[]>, Promise<BootstrapKeyOut[]>] = [
      machineApi.pending(),
      machineApi.cloneAlerts(),
      bootstrapKeyApi.list(),
    ]
    const [pendingRows, cloneRows, keyRows] = await Promise.all(tasks)
    pending.value = pendingRows
    alerts.value = cloneRows
    keys.value = keyRows
    players.value = contest.currentId ? await playerApi.list(contest.currentId) : []
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    loading.value = false
  }
}

void load()

// --------------------------------------------------------------------------- //
// 配对
// --------------------------------------------------------------------------- //

const pairCode = ref('')
const targetPlayerId = ref<number | null>(null)
const claiming = ref(false)

/**
 * 选中的选手是否已经有机器了。
 *
 * 服务端会拒绝（一人一机），但界面上提前标出来能省掉一次来回 ——
 * 而且这个信息本来就该让教师看见：它意味着"这台机器要换人"，
 * 那是个需要慎重判断的动作，不是随手一点。
 */
const playersWithMachine = computed(
  () => new Set(players.value.filter((p) => p.has_agent).map((p) => p.id)),
)

async function claimByCode(): Promise<void> {
  const code = pairCode.value.trim().toUpperCase()
  if (!code) {
    ElMessage.warning('请输入机器上显示的配对码')
    return
  }
  if (!targetPlayerId.value) {
    ElMessage.warning('请选择要配对给哪位选手')
    return
  }

  claiming.value = true
  try {
    const ack = await machineApi.claimByCode(code, targetPlayerId.value)
    ElMessage.success(ack.detail ?? '已配对')
    pairCode.value = ''
    targetPlayerId.value = null
    await Promise.all([load(), contest.load()])
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    claiming.value = false
  }
}

async function claimFromList(row: PendingMachineOut): Promise<void> {
  if (!targetPlayerId.value) {
    ElMessage.warning('先在下面选一位选手，再点「配对」')
    return
  }
  const player = players.value.find((p) => p.id === targetPlayerId.value)
  try {
    await ElMessageBox.confirm(
      `把「${row.hostname || row.machine_id}」配对给 ${player?.player_no ?? ''}？\n\n` +
        '如果你不在那台机器前面，建议改用配对码 —— 主机名可能是重复的，' +
        '而配对码是唯一能证明"这台就是那台"的东西。',
      '按主机名配对',
      { type: 'warning', confirmButtonText: '配对', cancelButtonText: '取消' },
    )
  } catch {
    return
  }

  claiming.value = true
  try {
    const ack = await machineApi.claimById(row.id, targetPlayerId.value)
    ElMessage.success(ack.detail ?? '已配对')
    await Promise.all([load(), contest.load()])
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    claiming.value = false
  }
}

async function revoke(row: PendingMachineOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `把「${row.hostname || row.machine_id}」从待配对列表里移除？\n\n` +
        '它下次心跳会拿到 401，然后重新注册一遍（会拿到新的配对码）。\n' +
        '要挡住它反复回来，得先吊销统一密钥。',
      '移除待配对机器',
      { type: 'warning', confirmButtonText: '移除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await machineApi.revokePending(row.id)
    ElMessage.success('已移除')
    await load()
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

// --------------------------------------------------------------------------- //
// 统一密钥
// --------------------------------------------------------------------------- //

const issuedKey = ref<BootstrapKeyIssuedOut | null>(null)
const keyDialog = ref(false)

async function issueKey(): Promise<void> {
  let label: string
  let expiresDays: number | null
  try {
    const result = await ElMessageBox.prompt(
      '用途备注，比如「2025 机房镜像」。密钥明文只会显示这一次。',
      '签发统一密钥',
      { confirmButtonText: '签发', cancelButtonText: '取消', inputValue: '' },
    )
    label = result.value.trim()
    expiresDays = null
  } catch {
    return
  }

  try {
    issuedKey.value = await bootstrapKeyApi.issue({
      label: label || null,
      note: null,
      expires_days: expiresDays,
    })
    keyDialog.value = true
    await load()
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

async function copyKey(): Promise<void> {
  const value = issuedKey.value?.key
  if (!value) return
  try {
    await navigator.clipboard.writeText(value)
    ElMessage.success('已复制到剪贴板')
  } catch {
    ElMessage.warning('复制失败，请手工选中复制')
  }
}

async function revokeKey(key: BootstrapKeyOut): Promise<void> {
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
  try {
    await bootstrapKeyApi.revoke(key.id)
    ElMessage.success('已吊销')
    await load()
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

function keyState(key: BootstrapKeyOut): { label: string; type: 'success' | 'info' | 'warning' } {
  if (key.revoked_at) return { label: '已吊销', type: 'info' }
  if (key.expires_at && new Date(key.expires_at) < new Date()) {
    return { label: '已过期', type: 'warning' }
  }
  return { label: '有效', type: 'success' }
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h2 class="page-title">机器配对</h2>
        <p class="page-hint">
          用镜像统一密钥注册上来的机器<strong>还没有归属</strong> ——
          服务端不知道它是哪个学生。把机器桌面上显示的配对码输进来，
          认领到名单里的某位选手，它才开始收代码。
        </p>
      </div>
      <div class="toolbar">
        <el-button size="small" :loading="loading" @click="load">刷新</el-button>
        <el-button size="small" @click="issueKey">签发统一密钥</el-button>
      </div>
    </div>

    <!-- 克隆镜像告警：多台机器共用同一个硬件指纹，是"镜像在跑过之后才克隆"的信号 -->
    <el-alert
      v-for="alert in alerts"
      :key="alert.fingerprint"
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom: 12px"
    >
      <template #title>
        疑似克隆镜像：{{ alert.machine_count }} 台机器共用同一个硬件指纹
      </template>
      <template #default>
        <div class="mono">{{ alert.fingerprint }}</div>
        <div>{{ (alert.hostnames ?? []).join('、') }}</div>
        <div class="page-hint">
          正常情况每台物理机的 SMBIOS UUID 都不同。撞了指纹说明镜像是<strong>在某台机器跑过之后</strong>才克隆的 ——
          那批机器里可能已经有人的凭据被一起拷了进去。建议逐台确认配对，
          别用「按主机名配对」批量点。
        </div>
      </template>
    </el-alert>

    <el-card shadow="never" style="margin-bottom: 12px">
      <div class="claim-row">
        <span class="field-label">配对码</span>
        <el-input
          v-model="pairCode"
          placeholder="机器桌面上「配对码.txt」里的 6 位码"
          style="width: 220px"
          class="mono-input"
          @keyup.enter="claimByCode"
        />
        <span class="field-label">配给</span>
        <el-select
          v-model="targetPlayerId"
          placeholder="选择选手"
          filterable
          style="width: 240px"
        >
          <el-option
            v-for="player in players"
            :key="player.id"
            :label="`${player.player_no}${player.name ? ' ' + player.name : ''}${
              playersWithMachine.has(player.id) ? '（已有机器）' : ''
            }`"
            :value="player.id"
          />
        </el-select>
        <el-button type="primary" size="default" :loading="claiming" @click="claimByCode">
          配对
        </el-button>
        <span class="page-hint">
          选手列表来自当前场次「{{ contest.current?.name ?? '未选场次' }}」，
          顶栏可以切换。
        </span>
      </div>
    </el-card>

    <el-table :data="pending" v-loading="loading" border stripe size="small">
      <el-table-column label="主机名" width="160">
        <template #default="{ row }">
          <span>{{ row.hostname || '—' }}</span>
        </template>
      </el-table-column>

      <el-table-column label="机器码" width="180">
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

      <el-table-column label="配对码" width="130">
        <template #default="{ row }">
          <!-- 过期了就直说：教师输一个过期码会得到 404，不如在这里就看见 -->
          <el-tag v-if="row.pair_code_expires_in !== null && row.pair_code_expires_in <= 0" type="danger" size="small" effect="plain">
            已过期
          </el-tag>
          <span v-else class="cell-sub">
            还有 {{ Math.max(0, Math.round((row.pair_code_expires_in ?? 0) / 60)) }} 分钟有效
          </span>
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

      <el-table-column label="客户端" min-width="150">
        <template #default="{ row }">
          <span class="cell-sub">版本 {{ row.agent_version || '未知' }}</span>
        </template>
      </el-table-column>

      <el-table-column label="操作" width="140" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="claimFromList(row)">配对</el-button>
          <el-button link type="danger" size="small" @click="revoke(row)">移除</el-button>
        </template>
      </el-table-column>

      <template #empty>
        <div class="empty-block">
          <p>没有待配对的机器。</p>
          <p class="page-hint">
            用统一密钥装好的机器开机后会自动出现在这里。如果一台都没出现，
            先确认镜像里放了 <code>bootstrap.key</code>、并且装了
            <code>syncoj-enroll.service</code>。
          </p>
        </div>
      </template>
    </el-table>

    <h3 class="section-title">统一注册密钥</h3>
    <p class="page-hint" style="margin-bottom: 10px">
      密钥能注册<strong>整间机房</strong>，所以它在机器上只是 root 只读的一份文件，
      绝不写进 <code>agent.ini</code>（那个文件对选手账号可读）。
      明文只在签发时显示一次。
    </p>

    <el-table :data="keys" border stripe size="small">
      <el-table-column label="用途" min-width="180">
        <template #default="{ row }">{{ row.label || '—' }}</template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="keyState(row).type" size="small" effect="plain">
            {{ keyState(row).label }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="用过" width="90" align="right">
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
      <el-table-column label="操作" width="90" fixed="right">
        <template #default="{ row }">
          <el-button
            link
            type="danger"
            size="small"
            :disabled="!!row.revoked_at"
            @click="revokeKey(row)"
          >
            吊销
          </el-button>
        </template>
      </el-table-column>

      <template #empty>
        <div class="empty-block">
          <p>还没有统一密钥。</p>
          <p class="page-hint">
            如果你打算"做一份镜像装遍整间机房"，就先签发一把，
            装机时用 <code>--bootstrap-key</code> 传给安装器。
          </p>
          <el-button type="primary" size="small" @click="issueKey">签发一把</el-button>
        </div>
      </template>
    </el-table>

    <el-dialog v-model="keyDialog" title="统一密钥（只显示这一次）" width="620px">
      <el-alert type="warning" :closable="false" show-icon style="margin-bottom: 12px">
        <template #title>现在抄下来 —— 关掉这个窗口就再也看不到明文了</template>
        <template #default>
          库里只存哈希。丢了就重新签发一把，然后把旧的吊销。
        </template>
      </el-alert>

      <div class="key-box mono">{{ issuedKey?.key }}</div>

      <div class="page-hint" style="margin-top: 12px">
        装机时这样用（它会被写成 <code>/etc/syncoj/bootstrap.key</code>，0600、属主 root）：
        <pre class="cmd">python3 install.py --bundle ./syncoj-agent-x.tar.gz \
    --server https://10.0.0.1:8443 \
    --bootstrap-key {{ issuedKey?.key }}</pre>
        安装器还会装上 <code>syncoj-enroll.service</code>：开机时由 root 用这把密钥
        换回本机凭据，之后 Agent 只读凭据，学生账号碰不到密钥。
      </div>

      <template #footer>
        <el-button @click="copyKey">复制</el-button>
        <el-button type="primary" @click="keyDialog = false">我已抄好</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.claim-row {
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
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  letter-spacing: 2px;
  text-transform: uppercase;
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
  margin: 22px 0 6px;
}
</style>

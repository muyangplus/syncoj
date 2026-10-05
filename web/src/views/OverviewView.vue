<script setup lang="ts">
import { computed, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'

import { agentApi, playerApi } from '@/api'
import type { AgentRuntimeOut, EnrollCodeOut, PlayerOut } from '@/api/types'
import { usePolling } from '@/composables/usePolling'
import { useContestStore } from '@/stores/contest'
import { formatBytes, formatSince, formatTime } from '@/utils/format'

const contest = useContestStore()
const { currentId } = storeToRefs(contest)

const players = ref<PlayerOut[]>([])
const agents = ref<AgentRuntimeOut[]>([])
const loading = ref(false)
const error = ref<string | null>(null)

const keyword = ref('')
const onlyOffline = ref(false)

/** 签发出来的注册码。只在这里展示一次，所以用对话框而不是表格列。 */
const issuedCode = ref<EnrollCodeOut | null>(null)
const codeDialog = ref(false)

const agentByPlayer = computed(() => {
  const map = new Map<number, AgentRuntimeOut>()
  for (const agent of agents.value) map.set(agent.player_id, agent)
  return map
})

const rows = computed(() => {
  const needle = keyword.value.trim().toLowerCase()
  return players.value
    .filter((player) => {
      if (onlyOffline.value && player.online) return false
      if (!needle) return true
      return (
        player.player_no.toLowerCase().includes(needle) ||
        (player.name ?? '').toLowerCase().includes(needle)
      )
    })
    .map((player) => ({ player, agent: agentByPlayer.value.get(player.id) ?? null }))
})

const summary = computed(() => {
  const total = players.value.length
  const online = players.value.filter((p) => p.online).length
  const withFiles = players.value.filter((p) => p.file_count > 0).length
  return { total, online, offline: total - online, withFiles }
})

async function refresh(): Promise<void> {
  if (!currentId.value) {
    players.value = []
    agents.value = []
    return
  }
  try {
    const [playerList, agentList] = await Promise.all([
      playerApi.list(currentId.value),
      agentApi.list(currentId.value),
    ])
    players.value = playerList
    agents.value = agentList
    error.value = null
  } catch (err) {
    error.value = (err as Error).message
  } finally {
    loading.value = false
  }
}

// 5 秒一刷：在线状态与最后心跳是这页的全部意义
usePolling(refresh, { interval: 5000, immediate: true })

async function issueCode(player: PlayerOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `为选手 ${player.player_no} 签发新的注册码？` +
        '此前未使用的注册码会立即失效（已经注册过的机器不受影响）。',
      '签发注册码',
      { confirmButtonText: '签发', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    issuedCode.value = await playerApi.issueEnrollCode(player.id)
    codeDialog.value = true
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

async function copyCode(): Promise<void> {
  const code = issuedCode.value?.code
  if (!code) return
  try {
    await navigator.clipboard.writeText(code)
    ElMessage.success('已复制到剪贴板')
  } catch {
    // 非 HTTPS 或浏览器策略下剪贴板不可用，提示手工复制而不是静默失败
    ElMessage.warning('浏览器不允许自动复制，请手工选中复制')
  }
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h2 class="page-title">选手状态</h2>
        <p class="page-hint">
          每 5 秒自动刷新。在线判定为服务端侧超时判断，与浏览器无关。
        </p>
      </div>
      <div class="toolbar">
        <el-input
          v-model="keyword"
          size="small"
          placeholder="搜索编号或姓名"
          clearable
          style="width: 180px"
        />
        <el-checkbox v-model="onlyOffline" size="small">只看离线</el-checkbox>
        <el-button size="small" :loading="loading" @click="refresh">刷新</el-button>
      </div>
    </div>

    <el-alert
      v-if="error"
      type="error"
      :closable="false"
      show-icon
      :title="error"
      style="margin-bottom: 12px"
    />

    <el-row :gutter="12" class="stats">
      <el-col :span="6">
        <el-card shadow="never">
          <div class="stat-value">{{ summary.online }}<span class="stat-unit">/{{ summary.total }}</span></div>
          <div class="stat-label">在线选手</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never">
          <div class="stat-value warn">{{ summary.offline }}</div>
          <div class="stat-label">当前离线</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never">
          <div class="stat-value">{{ summary.withFiles }}</div>
          <div class="stat-label">已回收代码</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never">
          <div class="stat-value">{{ agents.length }}</div>
          <div class="stat-label">已注册客户端</div>
        </el-card>
      </el-col>
    </el-row>

    <el-table :data="rows" v-loading="loading" border stripe size="small" style="margin-top: 12px">
      <el-table-column label="选手" width="150">
        <template #default="{ row }">
          <div class="mono">{{ row.player.player_no }}</div>
          <div class="cell-sub">{{ row.player.name || '—' }}</div>
        </template>
      </el-table-column>

      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <span>
            <span class="status-dot" :class="row.player.online ? 'online' : 'offline'" />
            <el-tag :type="row.player.online ? 'success' : 'info'" size="small" effect="plain">
              {{ row.player.online ? '在线' : '离线' }}
            </el-tag>
          </span>
        </template>
      </el-table-column>

      <el-table-column label="最后心跳" width="150">
        <template #default="{ row }">
          <div>{{ formatSince(row.agent?.seconds_since_tick, '从未') }}</div>
          <div class="cell-sub">{{ formatTime(row.player.last_tick_at) }}</div>
        </template>
      </el-table-column>

      <el-table-column label="代码文件" width="90" align="right">
        <template #default="{ row }">
          <span :class="{ muted: row.player.file_count === 0 }">{{ row.player.file_count }}</span>
        </template>
      </el-table-column>

      <el-table-column label="客户端" min-width="220">
        <template #default="{ row }">
          <template v-if="row.agent">
            <div class="mono">{{ row.agent.hostname || '—' }}</div>
            <div class="cell-sub">
              版本 {{ row.agent.agent_version || '未知' }}
              <span v-if="row.agent.scan_root"> · {{ row.agent.scan_root }}</span>
            </div>
          </template>
          <span v-else class="muted">未注册</span>
        </template>
      </el-table-column>

      <el-table-column label="机器码" width="150">
        <template #default="{ row }">
          <span class="mono muted">{{ row.agent?.machine_id?.slice(0, 16) || '—' }}</span>
        </template>
      </el-table-column>

      <el-table-column label="磁盘剩余" width="100" align="right">
        <template #default="{ row }">
          <span class="cell-sub">{{ formatBytes(row.agent?.disk_free ?? null) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="异常" min-width="180">
        <template #default="{ row }">
          <el-tooltip v-if="row.agent?.last_error" :content="row.agent.last_error">
            <span class="error-text">{{ row.agent.last_error }}</span>
          </el-tooltip>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>

      <el-table-column label="操作" width="110" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="issueCode(row.player)">
            签发注册码
          </el-button>
        </template>
      </el-table-column>

      <template #empty>
        <div class="empty-block">
          {{ players.length === 0 ? '本场次还没有导入选手' : '没有匹配的选手' }}
        </div>
      </template>
    </el-table>

    <el-dialog v-model="codeDialog" title="注册码" width="520px">
      <template v-if="issuedCode">
        <el-alert
          type="warning"
          :closable="false"
          show-icon
          title="这个注册码只显示这一次"
          description="请立即记录。它长期有效且可重复使用 —— 考试机被快照还原后，Agent 靠它自动重新注册，无需人工干预。"
        />
        <div class="code-box">
          <span class="code mono">{{ issuedCode.code }}</span>
        </div>
        <p class="page-hint">
          选手：{{ issuedCode.player_no }} ·
          使用方式：写入考试机的 <code>/etc/syncoj/agent.ini</code> 的
          <code>enroll_code</code> 字段，或安装时通过 <code>--enroll-code</code> 传入。
        </p>
      </template>
      <template #footer>
        <el-button @click="codeDialog = false">关闭</el-button>
        <el-button type="primary" @click="copyCode">复制</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.stats {
  margin-bottom: 4px;
}

.stat-value {
  font-size: 22px;
  font-weight: 600;
  line-height: 1.2;
}

.stat-value.warn {
  color: #e6a23c;
}

.stat-unit {
  font-size: 14px;
  color: #909399;
  font-weight: 400;
}

.stat-label {
  font-size: 12px;
  color: #909399;
  margin-top: 2px;
}

.error-text {
  color: #f56c6c;
  font-size: 12px;
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: middle;
}

.code-box {
  margin: 16px 0;
  padding: 14px;
  background: #f5f7fa;
  border-radius: 4px;
  text-align: center;
}

.code {
  font-size: 20px;
  letter-spacing: 2px;
  font-weight: 600;
}
</style>

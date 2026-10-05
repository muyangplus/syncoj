<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { agentApi, playerApi, scoreApi } from '@/api'
import type { AgentRuntimeOut, EnrollCodeOut, PlayerOut } from '@/api/types'
import PlayerImportDialog from '@/components/PlayerImportDialog.vue'
import { useContestData } from '@/composables/useContestData'
import { useContestStore } from '@/stores/contest'
import { formatBytes, formatSince, formatTime } from '@/utils/format'

const contest = useContestStore()

/**
 * 加载交给 useContestData 统一驱动：场次确定/变化时立刻加载，之后定时兜底刷新。
 *
 * **不要自己写 onMounted + setInterval** —— 组件挂载的时刻 `currentId` 往往还没
 * 从场次接口回来，首屏会拉个空，要等下一轮轮询才补上；刷新页面时最明显。
 */
const { data, loading, error, reload } = useContestData(
  async (contestId) => {
    const [players, agents, matrix] = await Promise.all([
      playerApi.list(contestId),
      agentApi.list(contestId),
      scoreApi.matrix(contestId),
    ])
    return { players, agents, matrix }
  },
  { interval: 5000 },
)

const players = computed<PlayerOut[]>(() => data.value?.players ?? [])
const agents = computed<AgentRuntimeOut[]>(() => data.value?.agents ?? [])

/**
 * 每位选手的「交题进度」。
 *
 * 巡检时要回答的是**"谁的座位该去看一眼"**，光看"回收了几个文件"答不了 ——
 * 交了一堆草稿和一个都没交，文件数看起来可能一样。
 *
 * 只统计**清单里登记过的题目**：未登记的题目可能只是历史遗留，
 * 拿它去质问选手是错的（和成绩页的口径保持一致）。
 */
const progressByPlayer = computed(() => {
  const matrix = data.value?.matrix
  const map = new Map<number, { submitted: number; missing: number; pending: number; total: number }>()
  if (!matrix) return map

  const declared = new Set(
    (matrix.columns ?? []).filter((c) => c.declared).map((c) => c.ident),
  )
  const total = declared.size

  for (const row of matrix.rows ?? []) {
    let submitted = 0
    let missing = 0
    let pending = 0
    for (const cell of row.cells ?? []) {
      if (!declared.has(cell.problem)) continue
      if (cell.parse_status === 'missing') {
        // 交了没评测 vs 根本没交 —— 前者不用管，后者要去找人
        if (cell.submitted) pending += 1
        else missing += 1
      } else {
        submitted += 1
      }
    }
    map.set(row.player_id, { submitted, missing, pending, total })
  }
  return map
})

const keyword = ref('')
const onlyOffline = ref(false)

const importVisible = ref(false)

const issuedCode = ref<EnrollCodeOut | null>(null)
const codeDialog = ref(false)

const batchDialog = ref(false)
const batchIssuing = ref(false)
const batchCodes = ref<EnrollCodeOut[]>([])

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
    .map((player) => ({
      player,
      agent: agentByPlayer.value.get(player.id) ?? null,
      progress: progressByPlayer.value.get(player.id) ?? null,
    }))
})

const summary = computed(() => {
  const total = players.value.length
  const online = players.value.filter((p) => p.online).length
  const withFiles = players.value.filter((p) => p.file_count > 0).length
  return { total, online, offline: total - online, withFiles }
})

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

async function issueForAllUnregistered(): Promise<void> {
  const pending = players.value.filter((player) => !player.has_agent)
  if (!pending.length) {
    ElMessage.info('所有选手都已经注册过了')
    return
  }
  try {
    await ElMessageBox.confirm(
      `为 ${pending.length} 名尚未注册的选手各签发一个注册码？\n\n` +
        '每个选手此前未使用的注册码会立即失效（已经注册过的机器不受影响）。',
      '批量签发注册码',
      { type: 'warning', confirmButtonText: '签发', cancelButtonText: '取消' },
    )
  } catch {
    return
  }

  batchIssuing.value = true
  batchCodes.value = []
  try {
    // 串行而不是并发：注册码要落库，五十个并发写 SQLite 没意义还会撞锁
    for (const player of pending) {
      try {
        batchCodes.value.push(await playerApi.issueEnrollCode(player.id))
      } catch (err) {
        ElMessage.error(`${player.player_no} 签发失败：${(err as Error).message}`)
      }
    }
    batchDialog.value = true
    if (batchCodes.value.length) {
      ElMessage.success(`已签发 ${batchCodes.value.length} 个注册码`)
    }
  } finally {
    batchIssuing.value = false
  }
}

async function copyBatchCodes(): Promise<void> {
  const text = batchCodes.value.map((item) => `${item.player_no}\t${item.code}`).join('\n')
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制全部（编号 + 注册码，制表符分隔）')
  } catch {
    ElMessage.warning('浏览器不允许自动复制，请手工选中复制')
  }
}

function exportBatchCodes(): void {
  const lines = ['选手编号,注册码', ...batchCodes.value.map((i) => `${i.player_no},${i.code}`)]
  const blob = new Blob(['\ufeff' + lines.join('\n')], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `enroll-codes-${contest.currentId ?? 'contest'}.csv`
  anchor.click()
  URL.revokeObjectURL(url)
}

function handleImported(): void {
  void reload()
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
        <el-button size="small" :loading="loading" @click="reload">刷新</el-button>
        <el-button size="small" type="primary" @click="importVisible = true">导入选手</el-button>
        <el-button
          size="small"
          :loading="batchIssuing"
          :disabled="!players.length"
          @click="issueForAllUnregistered"
        >
          批量签发注册码
        </el-button>
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
          <div class="stat-value">
            {{ summary.online }}<span class="stat-unit">/{{ summary.total }}</span>
          </div>
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

      <!-- 巡检要看的是"谁的座位该去看一眼"：交了几个文件答不了这个问题 -->
      <el-table-column label="交题进度" width="150">
        <template #default="{ row }">
          <el-tooltip
            v-if="row.progress"
            :content="
              `已出成绩 ${row.progress.submitted} 题 · ` +
              `已交待评测 ${row.progress.pending} 题 · ` +
              `未交 ${row.progress.missing} 题`
            "
          >
            <span>
              <el-tag v-if="row.progress.missing > 0" size="small" type="warning" effect="plain">
                未交 {{ row.progress.missing }}
              </el-tag>
              <el-tag v-else size="small" type="success" effect="plain">已交齐</el-tag>
              <span class="cell-sub mono">
                {{ row.progress.submitted + row.progress.pending }}/{{ row.progress.total }}
              </span>
            </span>
          </el-tooltip>
          <span v-else class="muted">—</span>
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
          <template v-if="players.length === 0">
            <p>本场次还没有导入选手</p>
            <el-button type="primary" size="small" @click="importVisible = true">
              导入选手
            </el-button>
          </template>
          <template v-else>没有匹配的选手</template>
        </div>
      </template>
    </el-table>

    <PlayerImportDialog
      v-model="importVisible"
      :contest-id="contest.currentId"
      @imported="handleImported"
    />

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

    <el-dialog v-model="batchDialog" title="批量签发的注册码" width="620px">
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="这些注册码只显示这一次"
        description="关闭本对话框后就再也看不到了（服务端只存哈希）。请立即复制或导出保存。"
      />
      <el-table :data="batchCodes" size="small" border max-height="360" style="margin-top: 12px">
        <el-table-column label="选手编号" prop="player_no" width="120">
          <template #default="{ row }">
            <span class="mono">{{ row.player_no }}</span>
          </template>
        </el-table-column>
        <el-table-column label="注册码" prop="code">
          <template #default="{ row }">
            <span class="mono">{{ row.code }}</span>
          </template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button @click="batchDialog = false">关闭</el-button>
        <el-button @click="exportBatchCodes">导出 CSV</el-button>
        <el-button type="primary" @click="copyBatchCodes">复制全部</el-button>
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

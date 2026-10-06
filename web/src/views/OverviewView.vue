<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessageBox } from 'element-plus'

import { agentApi, authApi, playerApi, rosterApi, scoreApi } from '@/api'
import type { AdminHealth } from '@/api'
import type { AgentRuntimeOut, PlayerOut, ScoreMatrixOut } from '@/api/types'
import ConfirmByNameDialog from '@/components/ConfirmByNameDialog.vue'
import DataTable from '@/components/DataTable.vue'
import FormDialog from '@/components/FormDialog.vue'
import PageShell from '@/components/PageShell.vue'
import PlayerImportDialog from '@/components/PlayerImportDialog.vue'
import RosterPersonPicker from '@/components/RosterPersonPicker.vue'
import { useList } from '@/composables/useList'
import { useMutation } from '@/composables/useMutation'
import { useContestStore } from '@/stores/contest'
import { formatBytes, formatSince, formatTime } from '@/utils/format'

const contest = useContestStore()

/**
 * 机器与成绩矩阵**跟着选手列表一起取**。
 *
 * 它们不是分页资源（一台机器的在线状态只有一份），而这一页要回答的是
 * "谁的座位该去看一眼" —— 三份数据必须来自同一个瞬间。分开轮询会出现
 * "选手显示在线、但机器那一列还是上一轮的名字"这种自相矛盾的画面，
 * 而教师在用的正是这种画面做判断。
 */
const agents = ref<AgentRuntimeOut[]>([])
const matrix = ref<ScoreMatrixOut | null>(null)
const health = ref<AdminHealth | null>(null)

const list = useList<PlayerOut>({
  rowKey: (row) => row.id,
  loader: async ({ limit, offset, signal }) => {
    const contestId = contest.currentId
    // 还没选场次：列表空着但不报错 —— "没选场次"不是错误
    if (!contestId) return null
    const [players, agentRows, scoreMatrix, serverHealth] = await Promise.all([
      playerApi.list(contestId, { limit, offset }, signal),
      // 机器列表也走信封；一个场次的机器数量天然有界，一次取满
      agentApi.list(contestId, { limit: 500 }, signal),
      scoreApi.matrix(contestId),
      // 服务端级的总览：它跨所有场次，与这一页的统计口径不同，所以要分开说
      authApi.health().catch(() => null),
    ])
    agents.value = agentRows.items
    matrix.value = scoreMatrix
    health.value = serverHealth
    return players
  },
  // 5 秒一轮：教师盯着看"谁掉线了"，这是最需要新鲜度的一页
  interval: 5000,
  watches: [() => contest.currentId],
})

// --------------------------------------------------------------------------- //
// 关键词是本页内的过滤（服务端选手列表没有关键词参数）
// --------------------------------------------------------------------------- //

const keyword = ref('')
const onlyOffline = ref(false)

const agentByPlayer = computed(() => {
  const map = new Map<number, AgentRuntimeOut>()
  for (const agent of agents.value) map.set(agent.player_id, agent)
  return map
})

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
  const map = new Map<
    number,
    { submitted: number; missing: number; pending: number; total: number }
  >()
  const current = matrix.value
  if (!current) return map

  const declared = new Set(
    (current.columns ?? []).filter((column) => column.declared).map((column) => column.ident),
  )
  const total = declared.size

  for (const row of current.rows ?? []) {
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

/**
 * 巡场时的排序方式。默认离线优先 —— 教师转一圈要找的是「谁的机器掉了」，
 * 而按考号平铺时掉线的人散落在整张表里。
 */
const sortMode = ref<'offline' | 'behind' | 'no'>('offline')

const rows = computed(() => {
  const needle = keyword.value.trim().toLowerCase()
  return list.rows.value
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
    // 排序只作用在**本页**：列表接口不收排序参数，跨页排序要服务端配合。
    // 所以页面说明里写的是「本页内」，而不是「整个场次最落后的那个在最上面」。
    .sort((a, b) => {
      if (sortMode.value === 'offline' && a.player.online !== b.player.online) {
        return a.player.online ? 1 : -1
      }
      if (sortMode.value !== 'no') {
        const behind = (b.progress?.missing ?? 0) - (a.progress?.missing ?? 0)
        if (behind !== 0) return behind
      }
      return a.player.player_no.localeCompare(b.player.player_no)
    })
})

/**
 * 清空选手的影响面。
 *
 * 最后一行跟着那个勾选框走 —— 关掉保护是会改变后果的动作，
 * 那一行必须在按下之前就变成红字含义的句子，而不是按完才从结果里看出来。
 */
const clearPlayersImpact = computed(() => [
  `本场次 ${list.total.value} 名选手会被清空`,
  clearKeepWithSubmissions.value
    ? '已有代码或成绩的选手会保留（保护开着）'
    : '已有代码或成绩的选手也会被删掉（保护已关闭）',
  '机器不受影响 —— 它们绑的是名单库里的人',
])

/** 删除单个选手的影响面。有几分提交是按成绩矩阵算出来的，不是猜的。 */
const deletePlayerImpact = computed(() => {
  const player = deleteTarget.value
  if (!player) return []
  const progress = progressByPlayer.value.get(player.id)
  const submitted = progress ? progress.submitted + progress.pending : 0
  return [
    `${player.player_no} 在本场次的代码台账与成绩会一起删`,
    submitted ? `他已有 ${submitted} 道题的提交` : '他还没有提交过代码',
    '机器不受影响 —— 要作废机器请在「机器」那一列里单独做',
  ]
})

const summary = computed(() => {
  const total = list.total.value
  const online = agents.value.filter((agent) => agent.online).length
  const withFiles = list.rows.value.filter((player) => player.file_count > 0).length
  return { total, online, offline: Math.max(0, total - online), withFiles }
})

/**
 * 表格行的标识。
 *
 * 表格里的行是"选手 + 机器 + 进度"拼出来的合成对象，而它的身份只取决于选手
 * —— 所以 rowKey 取选手 id。用合成对象的引用当 key 是不行的：每一轮 5 秒
 * 刷新都会重建这些对象。
 */
const tableRowKey = (row: { player: PlayerOut }) => row.player.id

// --------------------------------------------------------------------------- //
// 选手：编辑 / 删除 / 清空
// --------------------------------------------------------------------------- //

const importVisible = ref(false)

/** 最近一次「按默认名单补人」的回执。 */
const rosterReceipt = ref('')

/**
 * 按当前场次的默认名单补人。
 *
 * 放在**空状态**里，因为这里正是教师发现"怎么一个人都没有"的地方。此前这里写的
 * 是一句"也可以从「名单库」建一份名单，再用「应用到场次」"——那是解释，不是出路：
 * 它把人指向另一个页面，然后就没有下一步了。
 *
 * `prune` 固定 false：这个按钮只补人不删人。
 */
const fillFromDefaultRoster = useMutation(
  (rosterId: number) => {
    const id = contest.currentId
    if (id === null) throw new Error('还没有选中场次')
    return rosterApi.applyToContest(id, { roster_id: rosterId, prune: false })
  },
  {
    success: (report) => {
      const bits: string[] = []
      if (report.created) bits.push(`新建 ${report.created} 人`)
      if (report.updated) bits.push(`更新 ${report.updated} 人`)
      if (report.kept) bits.push(`保留 ${report.kept} 人`)
      rosterReceipt.value = bits.length ? `已补人：${bits.join('、')}` : '名单与场次一致，没有改动'
      return rosterReceipt.value
    },
    onDone: () => list.reload(),
  },
)

const editOpen = ref(false)
const editTarget = ref<PlayerOut | null>(null)
const editForm = ref({ player_no: '', name: '', seat: '', group_name: '' })

function askEdit(player: PlayerOut): void {
  editTarget.value = player
  editForm.value = {
    player_no: player.player_no,
    name: player.name ?? '',
    seat: player.seat ?? '',
    group_name: player.group_name ?? '',
  }
  editOpen.value = true
}

const savePlayer = useMutation(
  () => {
    const target = editTarget.value
    if (!target) throw new Error('没有选中选手')
    const form = editForm.value
    if (!form.player_no.trim()) throw new Error('考号不能为空')
    return playerApi.update(target.id, {
      player_no: form.player_no.trim(),
      name: form.name.trim() || null,
      seat: form.seat.trim() || null,
      group_name: form.group_name.trim() || null,
    })
  },
  {
    success: '已保存',
    onDone: async () => {
      editOpen.value = false
      editTarget.value = null
      await list.reload()
      await contest.load()
    },
  },
)

const deleteTarget = ref<PlayerOut | null>(null)
const deleteOpen = ref(false)

function askDelete(player: PlayerOut): void {
  deleteTarget.value = player
  deleteOpen.value = true
}

const deletePlayer = useMutation(() => {
  const target = deleteTarget.value
  if (!target) throw new Error('没有选中选手')
  return playerApi.remove(target.id, target.player_no)
}, {
  onDone: async () => {
    deleteOpen.value = false
    deleteTarget.value = null
    await list.reload()
    await contest.load()
  },
})

const clearOpen = ref(false)
const clearKeepWithSubmissions = ref(true)

const clearPlayers = useMutation(() => {
  const contestId = contest.currentId
  if (!contestId) throw new Error('还没有选场次')
  return playerApi.clear(
    contestId,
    contest.current?.slug ?? '',
    clearKeepWithSubmissions.value,
  )
}, {
  onDone: async () => {
    clearOpen.value = false
    clearKeepWithSubmissions.value = true
    await list.reload()
    await contest.load()
  },
})

// --------------------------------------------------------------------------- //
// 机器：解除绑定 / 改派 / 指定场次 / 作废
//
// 这些都在这一页而不是「机器配对」页：配对页管的是**还没有归属**的机器，
// 而这里的每一台都已经属于名单上的某个人 —— 屏幕上显示的归属就是下面这些人。
// --------------------------------------------------------------------------- //

const unbindTarget = ref<AgentRuntimeOut | null>(null)

const unbind = useMutation(() => {
  const target = unbindTarget.value
  if (!target) throw new Error('没有选中机器')
  return agentApi.unbind(target.agent_id)
}, {
  onDone: async () => {
    unbindTarget.value = null
    await list.reload()
  },
})

async function askUnbind(agent: AgentRuntimeOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `把 ${agent.player_no} 的这台机器解除绑定？\n\n` +
        '机器会回到「待配对」列表，凭据仍然有效；要换人请用「改派」。',
      '解除绑定',
      { type: 'warning', confirmButtonText: '解除绑定', cancelButtonText: '取消' },
    )
  } catch {
    // 取消：别把目标留着 —— 留着它会让下一次确认作用在这台机器上
    unbindTarget.value = null
    return
  }
  unbindTarget.value = agent
  await unbind.run(undefined)
}

const rebindOpen = ref(false)
const rebindTarget = ref<AgentRuntimeOut | null>(null)
const rebindEntryId = ref<number | null>(null)

function askRebind(agent: AgentRuntimeOut): void {
  rebindTarget.value = agent
  rebindEntryId.value = null
  rebindOpen.value = true
}

const rebind = useMutation(() => {
  const target = rebindTarget.value
  if (!target) throw new Error('没有选中机器')
  if (!rebindEntryId.value) throw new Error('请选择要改派给谁')
  return agentApi.rebind(target.agent_id, rebindEntryId.value)
}, {
  onDone: async () => {
    rebindOpen.value = false
    rebindTarget.value = null
    rebindEntryId.value = null
    await list.reload()
  },
})

const contestOpen = ref(false)
const contestTarget = ref<AgentRuntimeOut | null>(null)
/** `null` = 回到自动解析。 */
const contestChoice = ref<number | null>(null)

function askSetContest(agent: AgentRuntimeOut): void {
  contestTarget.value = agent
  // 现在的归属就是"自动解析"出来的，所以默认值也应当是"自动"：
  // 预填成当前场次会让教师以为它已经被显式指定过
  contestChoice.value = null
  contestOpen.value = true
}

const setContest = useMutation(() => {
  const target = contestTarget.value
  if (!target) throw new Error('没有选中机器')
  return agentApi.setContest(target.agent_id, contestChoice.value)
}, {
  onDone: async () => {
    contestOpen.value = false
    contestTarget.value = null
    await list.reload()
  },
})

const revokeOpen = ref(false)
const revokeTarget = ref<AgentRuntimeOut | null>(null)

function askRevokeAgent(agent: AgentRuntimeOut): void {
  revokeTarget.value = agent
  revokeOpen.value = true
}

const revokeAgent = useMutation(() => {
  const target = revokeTarget.value
  if (!target) throw new Error('没有选中机器')
  return agentApi.revoke(target.agent_id, target.hostname || target.machine_id)
}, {
  onDone: async () => {
    revokeOpen.value = false
    revokeTarget.value = null
    await list.reload()
  },
})

/** 单行操作走一个下拉：四个动作都放在表格里会把这一行挤到看不清数据。 */
function handleCommand(
  command: string,
  row: { player: PlayerOut; agent: AgentRuntimeOut | null },
): void {
  const agent = row.agent
  // 下拉本身只在这一列有值时出现，但模板里 narrowing 不过去，这里再挡一次
  if (!agent) return
  if (command === 'unbind') void askUnbind(agent)
  else if (command === 'rebind') askRebind(agent)
  else if (command === 'contest') askSetContest(agent)
  else if (command === 'revoke') askRevokeAgent(agent)
}
</script>

<template>
  <PageShell
    title="选手状态"
    :error="list.error.value"
    error-action="先重试；还不行就检查服务端。"
    retryable
    @retry="list.reload"
  >
    <template #sub>
      <!--
        这一行是**服务端级**的：它数的是整个库里所有场次的机器。与下面那排
        "本场次"的卡片口径不同，所以必须分开写，不能让两个数字混在一处。
      -->
      <p v-if="health" class="cell-sub">
        <span class="status-dot" :class="health.ok ? 'online' : 'offline'" />
        服务端{{ health.ok ? '正常' : '异常' }} · 全库机器 {{ health.agents_online }} /
        {{ health.agents_total }} 台在线 · 数据目录 <code>{{ health.data_root }}</code>
      </p>
    </template>

    <template #toolbar>
      <el-input
        v-model="keyword"
        size="small"
        placeholder="本页内搜编号或姓名"
        clearable
        style="width: 180px"
      />
      <el-select v-model="sortMode" size="small" style="width: 118px">
        <el-option label="离线优先" value="offline" />
        <el-option label="落后优先" value="behind" />
        <el-option label="按考号" value="no" />
      </el-select>
      <el-checkbox v-model="onlyOffline" size="small">只看离线</el-checkbox>
      <el-button size="small" :loading="list.loading.value" @click="list.reload">刷新</el-button>
      <el-button size="small" type="primary" @click="importVisible = true">导入选手</el-button>
      <el-button
        size="small"
        type="danger"
        plain
        :disabled="!list.total.value"
        @click="clearOpen = true"
      >
        清空选手
      </el-button>
    </template>

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
          <div class="stat-label">本页已回收代码</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never">
          <div class="stat-value">{{ agents.length }}</div>
          <div class="stat-label">已配对机器</div>
        </el-card>
      </el-col>
    </el-row>

    <DataTable
      :rows="rows"
      :row-key="tableRowKey"
      :loading="list.loading.value"
      :total="list.total.value"
      :page="list.page.value"
      :page-size="list.pageSize.value"
      style="margin-top: 12px"
      @update:page="list.setPage"
      @update:pageSize="list.setPageSize"
    >
      <template #columns>
        <el-table-column label="选手" width="150">
          <template #default="{ row }">
            <div class="mono">{{ row.player.player_no }}</div>
            <div class="cell-sub">{{ row.player.name || '—' }}</div>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <span>
              <span class="status-dot" :class="row.player.online ? 'online' : 'offline'" />
              <el-tag :type="row.player.online ? 'success' : 'info'" size="small" effect="plain">
                {{ row.player.online ? '在线' : '离线' }}
              </el-tag>
            </span>
          </template>
        </el-table-column>

        <el-table-column label="座位" width="90">
          <template #default="{ row }">
            <span class="cell-sub">{{ row.player.seat || '—' }}</span>
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

        <el-table-column label="机器" min-width="200">
          <template #default="{ row }">
            <template v-if="row.agent">
              <div class="mono">{{ row.agent.hostname || '—' }}</div>
              <div class="cell-sub">
                版本 {{ row.agent.agent_version || '未知' }}
                <span v-if="row.agent.scan_root"> · {{ row.agent.scan_root }}</span>
              </div>
            </template>
            <span v-else class="muted">未配对</span>
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

        <el-table-column label="异常" min-width="160">
          <template #default="{ row }">
            <el-tooltip v-if="row.agent?.last_error" :content="row.agent.last_error">
              <span class="error-text">{{ row.agent.last_error }}</span>
            </el-tooltip>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="askEdit(row.player)">
              编辑
            </el-button>
            <el-button link type="danger" size="small" @click="askDelete(row.player)">
              删除
            </el-button>
            <el-dropdown
              v-if="row.agent"
              trigger="click"
              @command="(command: string) => handleCommand(command, row)"
            >
              <el-button link type="primary" size="small">机器<el-icon><ArrowDown /></el-icon></el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="rebind">改派给另一个人</el-dropdown-item>
                  <el-dropdown-item command="setContest">指定场次</el-dropdown-item>
                  <el-dropdown-item command="unbind" divided>解除绑定</el-dropdown-item>
                  <el-dropdown-item command="revoke">作废这台机器</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </template>
        </el-table-column>
      </template>

      <template #empty>
        <template v-if="!list.rows.value.length">
          <p>本场次还没有选手</p>
          <div class="empty-actions">
            <!--
              有默认名单时，直接把「补人」摆在这里 —— 教师正是在这一页发现"人是空的"，
              让他为此再跑一趟「名单库」就是多出来的那一步。
            -->
            <el-button
              v-if="contest.current?.default_roster_id"
              type="primary"
              size="small"
              :loading="fillFromDefaultRoster.pending.value"
              @click="fillFromDefaultRoster.run(contest.current?.default_roster_id as number)"
            >
              按默认名单「{{ contest.current?.default_roster_name }}」补人
            </el-button>
            <el-button size="small" @click="importVisible = true">导入选手</el-button>
          </div>
        </template>
        <template v-else>本页没有匹配的选手</template>
      </template>
    </DataTable>

    <PlayerImportDialog
      v-model="importVisible"
      :contest-id="contest.currentId"
      @imported="() => list.reload()"
    />

    <!-- 编辑选手 -->
    <FormDialog
      v-model="editOpen"
      title="编辑选手"
      :submitting="savePlayer.pending.value"
      @submit="savePlayer.run(undefined)"
    >
      <el-form label-width="90px">
        <el-form-item label="考号">
          <el-input v-model="editForm.player_no" maxlength="64" />
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="editForm.name" maxlength="64" />
        </el-form-item>
        <el-form-item label="座位">
          <el-input v-model="editForm.seat" maxlength="32" />
        </el-form-item>
        <el-form-item label="分组">
          <el-input v-model="editForm.group_name" maxlength="64" />
        </el-form-item>
      </el-form>
      <el-alert type="info" :closable="false" show-icon>
        <template #title>改了考号，磁盘上的旧目录不会改名</template>
      </el-alert>
    </FormDialog>

    <!-- 删除选手：硬删除，打考号 -->
    <ConfirmByNameDialog
      v-model="deleteOpen"
      title="删除选手"
      :expected="deleteTarget?.player_no ?? ''"
      :submitting="deletePlayer.pending.value"
      :impact="deletePlayerImpact"
      @confirm="deletePlayer.run(undefined)"
    />

    <!-- 清空选手：范围删除，打场次标识 -->
    <ConfirmByNameDialog
      v-model="clearOpen"
      title="清空本场次的选手"
      :expected="contest.current?.slug ?? ''"
      :submitting="clearPlayers.pending.value"
      confirm-text="清空"
      :impact="clearPlayersImpact"
      @confirm="clearPlayers.run(undefined)"
    >
      <el-checkbox v-model="clearKeepWithSubmissions">
        保留已经有代码或成绩的选手（推荐）
      </el-checkbox>
      <el-alert v-if="!clearKeepWithSubmissions" type="error" :closable="false" show-icon>
        <template #title>关掉保护会把提交一起删掉，而且不可逆</template>
      </el-alert>
    </ConfirmByNameDialog>

    <!-- 改派：机器换人 -->
    <FormDialog
      v-model="rebindOpen"
      title="改派给另一个人"
      :submitting="rebind.pending.value"
      :disabled="!rebindEntryId"
      confirm-text="改派"
      @submit="rebind.run(undefined)"
    >
      <RosterPersonPicker
        v-model="rebindEntryId"
        :default-roster-id="contest.current?.default_roster_id ?? null"
      />
    </FormDialog>

    <!-- 指定场次：只在"同一个人同时在多场进行中的比赛里"才需要 -->
    <FormDialog
      v-model="contestOpen"
      title="指定场次"
      :submitting="setContest.pending.value"
      confirm-text="保存"
      @submit="setContest.run(undefined)"
    >
      <el-form label-width="80px">
        <el-form-item label="场次">
          <el-select v-model="contestChoice" clearable placeholder="自动匹配" style="width: 260px">
            <el-option
              v-for="item in contest.contests"
              :key="item.id"
              :label="`${item.name}（${item.slug}）`"
              :value="item.id"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="留空 = 自动匹配。"
      />
    </FormDialog>

    <!-- 作废机器：硬删除，打主机名 -->
    <ConfirmByNameDialog
      v-model="revokeOpen"
      title="作废这台机器"
      :expected="revokeTarget?.hostname || revokeTarget?.machine_id || ''"
      :submitting="revokeAgent.pending.value"
      confirm-text="作废"
      detail="凭据作废（下一轮心跳 401），要重新注册并配对；只想换人请用「改派」。"
      @confirm="revokeAgent.run(undefined)"
    />
  </PageShell>
</template>

<style scoped>
.empty-actions {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
}

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
</style>

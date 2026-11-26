<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'

import { contestApi, playerApi } from '@/api'
import type { ContestOut } from '@/api/types'
import ConfirmByNameDialog from '@/components/ConfirmByNameDialog.vue'
import ContestCreateDialog from '@/components/ContestCreateDialog.vue'
import ContestSettingsDialog from '@/components/ContestSettingsDialog.vue'
import DataTable from '@/components/DataTable.vue'
import ContestTabs from '@/components/ContestTabs.vue'
import PageShell from '@/components/PageShell.vue'
import SectionCard from '@/components/SectionCard.vue'
import ProblemManageDialog from '@/components/ProblemManageDialog.vue'
import { useList } from '@/composables/useList'
import { useMutation } from '@/composables/useMutation'
import { useContestStore } from '@/stores/contest'
import { contestStatusLabel, contestStatusType, formatTime } from '@/utils/format'

const contest = useContestStore()
const router = useRouter()
const route = useRoute()

/**
 * 本页表格自己取数，不动 store 里那份。
 *
 * store 里的 `contests` 是**顶栏下拉框**的数据源，它的刷新时机属于整个应用
 * （登录后拉一次）；而这里的列表要跟着"新建/改名/删除/清空选手"立刻更新。
 * 两者混在一起会让"刷新这一页"变成"刷新全局"，反过来也一样。
 * 代价是这两处要一起刷 —— 所以下面所有写操作都走 `refreshAll()` 这一个出口。
 */
const list = useList<ContestOut>({
  rowKey: (row) => row.id,
  // 已归档的场次不在这里列（侧栏「场次管理 → 已归档」）
  loader: ({ limit, offset, signal }) => contestApi.list({ limit, offset, archived: false }, signal),
})

/** 表格与顶栏下拉共用同一个"当前场次"，写操作之后两边都得刷。 */
async function refreshAll(): Promise<void> {
  await list.reload()
  // 取不到场次列表时不该再盖一层红字：表格自己已经报过它的错
  await contest.load().catch(() => undefined)
}

/**
 * 列表里的时间窗摘要：`MM-DD HH:mm → MM-DD HH:mm`。
 *
 * 两个都留空是**默认**、也是最容易看错的一种状态，所以它必须显示成"不限"而不是
 * 空白 —— 空白会被读成"这一列没数据"，而这一列恰恰决定"到点还收不收卷"。
 */
function windowLabel(row: ContestOut): string {
  const start = row.starts_at ? formatTime(row.starts_at).slice(5, 16) : '不限'
  const end = row.ends_at ? formatTime(row.ends_at).slice(5, 16) : '不限'
  return `${start} → ${end}`
}

// --------------------------------------------------------------------------- //
// 新建 / 设置 / 进入
// --------------------------------------------------------------------------- //

const createVisible = ref(false)
const settingsVisible = ref(false)
const settingsTarget = ref<ContestOut | null>(null)
const problemVisible = ref(false)
const problemContestId = ref<number | null>(null)

// 快捷操作入口：`?do=new` 打开新建场次弹窗
watch(
  () => route.query.do,
  (action) => {
    if (action !== 'new') return
    createVisible.value = true
    void router.replace({ query: { ...route.query, do: undefined } })
  },
  { immediate: true },
)

function openSettings(row: ContestOut): void {
  settingsTarget.value = row
  settingsVisible.value = true
}

function onRowCommand(command: string, row: ContestOut): void {
  if (command === 'clear') askClear(row)
  else if (command === 'delete') askDelete(row)
  else if (command === 'archive') void archive.run(row)
}

// --------------------------------------------------------------------------- //
// 归档：可逆，不要名称确认；进行中 / 封榜的场次服务端回 409，界面先禁掉
// --------------------------------------------------------------------------- //

function canArchive(row: ContestOut): boolean {
  return row.status !== 'running' && row.status !== 'frozen'
}

const archive = useMutation((row: ContestOut) => contestApi.archive(row.id), {
  success: (result) => `「${result.name}」已归档`,
  onDone: refreshAll,
})

function useContest(id: number): void {
  contest.select(id)
  ElMessage.success('已切换当前场次')
}

function openProblems(id: number): void {
  // 题目是挂在具体场次下的：先切过去再打开，
  // 避免"看着是这场、改的却是那场"
  contest.select(id)
  problemContestId.value = id
  problemVisible.value = true
}

function openPlayers(): void {
  void router.push({ name: 'overview' })
}

// --------------------------------------------------------------------------- //
// 删除场次
//
// 这是整个系统里破坏力最大的操作：外键全是 ON DELETE CASCADE，删一个场次会
// 连带删掉它的选手、代码台账、下发任务、成绩。所以走"把标识打一遍"的确认，
// 而不是"是否确定" —— 后者在连续操作里会被手指肌肉记忆点掉。
// --------------------------------------------------------------------------- //

const deleteOpen = ref(false)
const deleteTarget = ref<ContestOut | null>(null)

const removeContest = useMutation(
  () => {
    const row = deleteTarget.value
    if (!row) throw new Error('没有选中场次')
    // `confirm` 是场次标识（slug），服务端会逐字校验
    return contestApi.remove(row.id, row.slug)
  },
  {
    onDone: async () => {
      deleteOpen.value = false
      deleteTarget.value = null
      await refreshAll()
    },
  },
)

function askDelete(row: ContestOut): void {
  deleteTarget.value = row
  deleteOpen.value = true
}

// --------------------------------------------------------------------------- //
// 清空选手
//
// 默认 `keep_with_submissions = true`：已经有代码或成绩的选手留着。顺手把
// 参赛者的提交一起删掉是不可逆的、而且它不报错，所以"连提交一起清"必须是
// 显式勾选，不能是默认值。
// --------------------------------------------------------------------------- //

const clearOpen = ref(false)
const clearTarget = ref<ContestOut | null>(null)

/**
 * 删除场次前的影响面。
 *
 * 能数出来的只有这一场自己的统计（选手数、此刻在线的机器数）—— 代码文件数
 * 与成绩条数不在这条列表接口的返回里。数不出来的就不写，宁可少一行，
 * 也不要写一个看起来精确、其实是猜的数字。
 */
const deleteImpact = computed(() => {
  const row = deleteTarget.value
  if (!row) return []
  return [
    `选手 ${row.player_count} 名`,
    `在线机器 ${row.online_count} 台`,
  ]
})
const clearKeep = ref(true)

const clearPlayers = useMutation(
  () => {
    const row = clearTarget.value
    if (!row) throw new Error('没有选中场次')
    return playerApi.clear(row.id, row.slug, clearKeep.value)
  },
  {
    onDone: async () => {
      clearOpen.value = false
      clearTarget.value = null
      clearKeep.value = true
      await refreshAll()
    },
  },
)

function askClear(row: ContestOut): void {
  clearTarget.value = row
  clearKeep.value = true
  clearOpen.value = true
}

/** 确认文案里要写出真实人数 —— "清空"两个字答不了"要清掉几个人"。 */
function clearDetail(count: number): string {
  if (clearKeep.value) {
    return `将删除 ${count} 名选手中无代码、无成绩的选手，不可恢复。`
  }
  return `将删除全部 ${count} 名选手及其代码台账与成绩，不可恢复。`
}
</script>

<template>
  <PageShell title="场次管理" :error="list.error.value" retryable @retry="refreshAll">
    <template #sub><ContestTabs :archived-count="contest.archived.length" /></template>
    <template #toolbar>
      <el-button :loading="list.loading.value" @click="refreshAll">
        <el-icon><Refresh /></el-icon>刷新
      </el-button>
      <el-button type="primary" @click="createVisible = true">
        <el-icon><Plus /></el-icon>新建场次
      </el-button>
    </template>

    <SectionCard title="场次" :meta="`${list.total.value ?? 0} 场`" flush>
      <DataTable
        :rows="list.rows.value"
        :row-key="(row: ContestOut) => row.id"
        :loading="list.loading.value"
        :total="list.total.value"
        :page="list.page.value"
        :page-size="list.pageSize.value"
        empty-text="无场次"
        @update:page="list.setPage"
        @update:pageSize="list.setPageSize"
      >
        <template #columns>
          <el-table-column label="当前" width="80" align="center">
            <template #default="{ row }">
              <el-tag v-if="row.id === contest.currentId" type="primary" effect="light">使用中</el-tag>
              <el-button v-else link type="primary" size="small" @click="useContest(row.id)">
                切换
              </el-button>
            </template>
          </el-table-column>

          <el-table-column label="名称" min-width="200">
            <template #default="{ row }">
              <div>{{ row.name }}</div>
              <div class="cell-sub">
                标识 <span class="mono">{{ row.slug }}</span>
              </div>
            </template>
          </el-table-column>

          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag :type="contestStatusType(row.status)" effect="plain">
                {{ contestStatusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>

          <el-table-column label="考试时间" width="180">
            <template #default="{ row }">
              <el-tooltip :content="`${row.starts_at ?? '不限'} → ${row.ends_at ?? '不限'}`">
                <span :class="{ muted: !row.starts_at && !row.ends_at }">
                  {{ windowLabel(row) }}
                </span>
              </el-tooltip>
            </template>
          </el-table-column>

          <el-table-column label="选手" width="90" align="right">
            <template #default="{ row }">
              <span :class="{ muted: !row.player_count }">{{ row.player_count }} 名</span>
            </template>
          </el-table-column>

          <el-table-column label="在线" width="100" align="right">
            <template #default="{ row }">
              <span :class="{ 'online-text': row.online_count > 0 }">
                {{ row.online_count }} / {{ row.player_count }}
              </span>
            </template>
          </el-table-column>

          <el-table-column label="默认名单" width="140">
            <template #default="{ row }">
              <span v-if="row.default_roster_name">{{ row.default_roster_name }}</span>
              <span v-else class="muted">未指定</span>
            </template>
          </el-table-column>

          <el-table-column label="创建时间" width="170">
            <template #default="{ row }">
              <el-tooltip :content="row.created_at">
                <span class="cell-sub">{{ formatTime(row.created_at) }}</span>
              </el-tooltip>
            </template>
          </el-table-column>

          <el-table-column label="就绪" width="120">
            <template #default="{ row }">
              <el-tag v-if="row.player_count === 0" type="warning" effect="plain">无选手</el-tag>
              <el-tag v-else-if="row.online_count === 0" type="info" effect="plain">无机器在线</el-tag>
              <el-tag v-else type="success" effect="light">运行中</el-tag>
            </template>
          </el-table-column>

          <el-table-column label="操作" width="220" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openProblems(row.id)">
                题目
              </el-button>
              <el-button link type="primary" size="small" @click="openSettings(row)">
                设置
              </el-button>
              <el-button
                link
                type="primary"
                size="small"
                @click="useContest(row.id); openPlayers()"
              >
                进入
              </el-button>
              <el-dropdown
                trigger="click"
                @command="(command: string) => onRowCommand(command, row)"
              >
                <el-button link type="primary" size="small">
                  更多<el-icon class="el-icon--right"><ArrowDown /></el-icon>
                </el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="archive" :disabled="!canArchive(row)">
                      {{ canArchive(row) ? '归档' : '归档（先结束场次）' }}
                    </el-dropdown-item>
                    <el-dropdown-item command="clear" :disabled="row.player_count === 0">
                      <span class="danger-text">清空选手</span>
                    </el-dropdown-item>
                    <el-dropdown-item command="delete">
                      <span class="danger-text">删除场次</span>
                    </el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </template>
          </el-table-column>
        </template>

        <template #empty>
          <p>无场次</p>
          <el-button type="primary" style="margin-top: 12px" @click="createVisible = true">
            <el-icon><Plus /></el-icon>新建场次
          </el-button>
        </template>
      </DataTable>
    </SectionCard>

    <ContestCreateDialog v-model="createVisible" @created="refreshAll" />
    <ContestSettingsDialog
      v-model="settingsVisible"
      :contest="settingsTarget"
      @saved="refreshAll"
      @changed="refreshAll"
    />
    <ProblemManageDialog
      v-model="problemVisible"
      :contest-id="problemContestId"
      @changed="refreshAll"
    />

    <!-- 删除整场：标识逐字确认 -->
    <ConfirmByNameDialog
      v-model="deleteOpen"
      :title="`删除场次「${deleteTarget?.name ?? ''}」？`"
      :expected="deleteTarget?.slug ?? ''"
      :submitting="removeContest.pending.value"
      confirm-text="删除"
      :impact="deleteImpact"
      detail="将删除本场次及其选手、代码台账、下发任务、成绩、资产，不可恢复；机器与配对保留。"
      @confirm="removeContest.run(undefined)"
    />

    <!-- 清空选手：默认保留有提交的选手 -->
    <ConfirmByNameDialog
      v-model="clearOpen"
      title="清空选手？"
      :expected="clearTarget?.slug ?? ''"
      :submitting="clearPlayers.pending.value"
      confirm-text="清空"
      :detail="clearDetail(clearTarget?.player_count ?? 0)"
      @confirm="clearPlayers.run(undefined)"
    >
      <el-checkbox v-model="clearKeep">保留有代码或成绩的选手</el-checkbox>
    </ConfirmByNameDialog>
  </PageShell>
</template>

<style scoped>
.online-text {
  color: var(--el-color-success);
  font-weight: 600;
}
.danger-text {
  color: var(--el-color-danger);
}
</style>

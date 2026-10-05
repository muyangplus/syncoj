<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'

import { contestApi, playerApi } from '@/api'
import type { ContestOut } from '@/api/types'
import ConfirmByNameDialog from '@/components/ConfirmByNameDialog.vue'
import ContestCreateDialog from '@/components/ContestCreateDialog.vue'
import ContestSettingsDialog from '@/components/ContestSettingsDialog.vue'
import DataTable from '@/components/DataTable.vue'
import HelpTip from '@/components/HelpTip.vue'
import PageShell from '@/components/PageShell.vue'
import ProblemManageDialog from '@/components/ProblemManageDialog.vue'
import { useList } from '@/composables/useList'
import { useMutation } from '@/composables/useMutation'
import { useContestStore } from '@/stores/contest'
import { contestStatusLabel, contestStatusType, formatTime } from '@/utils/format'

const contest = useContestStore()
const router = useRouter()

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
  loader: ({ limit, offset, signal }) => contestApi.list({ limit, offset }, signal),
})

/** 表格与顶栏下拉共用同一个"当前场次"，写操作之后两边都得刷。 */
async function refreshAll(): Promise<void> {
  await list.reload()
  // 取不到场次列表时不该再盖一层红字：表格自己已经报过它的错
  await contest.load().catch(() => undefined)
}

// --------------------------------------------------------------------------- //
// 新建 / 设置 / 进入
// --------------------------------------------------------------------------- //

const createVisible = ref(false)
const settingsVisible = ref(false)
const settingsTarget = ref<ContestOut | null>(null)
const problemVisible = ref(false)
const problemContestId = ref<number | null>(null)

function openSettings(row: ContestOut): void {
  settingsTarget.value = row
  settingsVisible.value = true
}

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
    `这场有 ${row.player_count} 名选手`,
    `此刻有 ${row.online_count} 台机器在线`,
    '机器本身不受影响 —— 它们绑的是名单库里的人',
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
  const base = `这场共有 ${count} 名选手。`
  if (clearKeep.value) {
    return (
      base +
      '已经有代码或成绩的选手会被保留 —— 删掉他们的提交是不可逆的，' +
      '所以默认不动。没有提交的那部分会被删掉。'
    )
  }
  return (
    base +
    '这次连有提交的选手一起删：他们的代码台账与成绩会一并消失，无法恢复。'
  )
}
</script>

<template>
  <PageShell
    title="场次管理"
    hint="这里的操作只动被点的那一场。"
    :error="list.error.value"
    error-action="服务端可能没在跑，或登录已过期。"
    retryable
    @retry="refreshAll"
  >
    <template #hint>
      <HelpTip>
        选手、代码、下发任务、成绩都按场次隔离。顶栏的下拉框切换的是「当前操作哪个场次」。
      </HelpTip>
    </template>

    <template #toolbar>
      <el-button size="small" :loading="list.loading.value" @click="refreshAll">
        刷新
      </el-button>
      <el-button size="small" type="primary" @click="createVisible = true">
        新建场次
      </el-button>
    </template>

    <DataTable
      :rows="list.rows.value"
      :row-key="(row: ContestOut) => row.id"
      :loading="list.loading.value"
      :total="list.total.value"
      :page="list.page.value"
      :page-size="list.pageSize.value"
      empty-text="还没有任何场次"
      @update:page="list.setPage"
      @update:pageSize="list.setPageSize"
    >
      <template #columns>
        <el-table-column label="当前" width="70" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.id === contest.currentId" type="primary" size="small">
              使用中
            </el-tag>
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
            <el-tag :type="contestStatusType(row.status)" size="small" effect="plain">
              {{ contestStatusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="选手" width="90" align="right">
          <template #default="{ row }">
            <span :class="{ muted: !row.player_count }">{{ row.player_count }}</span>
          </template>
        </el-table-column>

        <el-table-column label="在线" width="90" align="right">
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

        <el-table-column label="创建时间" width="160">
          <template #default="{ row }">
            <!-- 表里只给到分钟，精确到秒的原始值留在 tooltip 里 -->
            <el-tooltip :content="row.created_at">
              <span class="cell-sub">{{ formatTime(row.created_at) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="下一步" min-width="180">
          <template #default="{ row }">
            <span v-if="row.player_count === 0" class="cell-sub">
              还没导入选手 —— 在「名单库」应用一份，或到「选手状态」页导入
            </span>
            <span v-else-if="row.online_count === 0" class="cell-sub">
              还没有考试机上线 —— 到「机器配对」看看有没有待认领的机器
            </span>
            <span v-else class="cell-sub">运行中</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="270" fixed="right">
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
            <el-button
              link
              type="warning"
              size="small"
              :disabled="row.player_count === 0"
              @click="askClear(row)"
            >
              清空选手
            </el-button>
            <el-tooltip content="删除整场 —— 连选手、代码台账与成绩一起删">
              <el-button link type="danger" size="small" @click="askDelete(row)">
                删除
              </el-button>
            </el-tooltip>
          </template>
        </el-table-column>
      </template>

      <template #empty>
        <p>还没有任何场次。</p>
        <p>还没有场次。先建一个，再导入选手。</p>
        <el-button type="primary" style="margin-top: 12px" @click="createVisible = true">
          新建第一个场次
        </el-button>
      </template>
    </DataTable>

    <ContestCreateDialog v-model="createVisible" @created="refreshAll" />
    <ContestSettingsDialog
      v-model="settingsVisible"
      :contest="settingsTarget"
      @saved="refreshAll"
    />
    <ProblemManageDialog
      v-model="problemVisible"
      :contest-id="problemContestId"
      @changed="refreshAll"
    />

    <!--
      删除整个场次。警报里那句"机器不受影响"是给教师吃的定心丸：机器绑的是
      名单里的**人**，不是这场比赛的记录，所以删完不用重新配 50 台机器。
    -->
    <ConfirmByNameDialog
      v-model="deleteOpen"
      title="删除场次"
      :expected="deleteTarget?.slug ?? ''"
      :submitting="removeContest.pending.value"
      confirm-text="删除场次"
      :impact="deleteImpact"
      :detail="
        `场次「${deleteTarget?.name ?? ''}」连同它的一切都会被删掉：` +
        '选手、代码台账、下发任务、成绩、资产都在里面，删完无法恢复。' +
        '机器不受影响 —— 它们绑的是名单库里的人，不是这场比赛的记录，' +
        '换一场比赛照样能用，不需要重新配对。'
      "
      @confirm="removeContest.run(undefined)"
    />

    <!--
      清空选手 = 范围删除，`confirm` 是场次标识。默认保护有提交的选手，
      那个复选框存在的意义就是让"连提交一起清"变成一次显式选择。
    -->
    <ConfirmByNameDialog
      v-model="clearOpen"
      title="清空选手名单"
      :expected="clearTarget?.slug ?? ''"
      :submitting="clearPlayers.pending.value"
      confirm-text="清空"
      :detail="clearDetail(clearTarget?.player_count ?? 0)"
      @confirm="clearPlayers.run(undefined)"
    >
      <div style="margin-bottom: 4px">
        <el-checkbox v-model="clearKeep">保留有代码或成绩的选手（推荐先留着）</el-checkbox>
      </div>
      <el-alert v-if="!clearKeep" type="error" :closable="false" show-icon>
        <template #title>注意：有提交的选手也会被删，他们的代码与成绩一起消失</template>
        <template #default>
          被删掉的提交没有回收站 —— 数据库里不会留任何副本。
          只有确认这批人不再需要复核成绩时才关掉这个保护。
        </template>
      </el-alert>
    </ConfirmByNameDialog>
  </PageShell>
</template>

<style scoped>
.online-text {
  color: #67c23a;
  font-weight: 600;
}
</style>

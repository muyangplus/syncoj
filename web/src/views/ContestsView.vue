<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'

import ContestCreateDialog from '@/components/ContestCreateDialog.vue'
import ProblemManageDialog from '@/components/ProblemManageDialog.vue'
import { useContestStore } from '@/stores/contest'
import { contestStatusLabel, contestStatusType, formatTime } from '@/utils/format'

const contest = useContestStore()
const router = useRouter()

const createVisible = ref(false)
const problemVisible = ref(false)
const problemContestId = ref<number | null>(null)

async function refresh(): Promise<void> {
  await contest.load()
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

function handleCreated(): void {
  void refresh()
}

function openPlayers(): void {
  void router.push({ name: 'overview' })
}

refresh()
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h2 class="page-title">场次管理</h2>
        <p class="page-hint">
          所有数据（选手、代码、下发任务、成绩）都按场次隔离。
          顶栏的下拉框切换的是"当前操作哪个场次"。
        </p>
      </div>
      <div class="toolbar">
        <el-button size="small" :loading="contest.loading" @click="refresh">刷新</el-button>
        <el-button size="small" type="primary" @click="createVisible = true">
          新建场次
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="contest.error"
      type="error"
      :closable="false"
      show-icon
      :title="contest.error"
      style="margin-bottom: 12px"
    />

    <el-table
      v-if="contest.contests.length"
      :data="contest.contests"
      v-loading="contest.loading"
      border
      stripe
      size="small"
    >
      <el-table-column label="当前" width="70" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.id === contest.currentId" type="primary" size="small">使用中</el-tag>
          <el-button
            v-else
            link
            type="primary"
            size="small"
            @click="useContest(row.id)"
          >
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

      <el-table-column label="创建时间" width="160">
        <template #default="{ row }">
          <span class="cell-sub">{{ formatTime(row.created_at) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="下一步" min-width="180">
        <template #default="{ row }">
          <span v-if="row.player_count === 0" class="cell-sub">
            还没导入选手 —— 切到该场次后在「选手状态」页导入
          </span>
          <span v-else-if="row.online_count === 0" class="cell-sub">
            还没有考试机上线 —— 需要为选手签发注册码并安装 Agent
          </span>
          <span v-else class="cell-sub">运行中</span>
        </template>
      </el-table-column>

      <el-table-column label="操作" width="170" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="openProblems(row.id)">
            题目
          </el-button>
          <el-button
            link
            type="primary"
            size="small"
            @click="useContest(row.id); openPlayers()"
          >
            进入
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-card v-else shadow="never">
      <div class="empty-block">
        <p>还没有任何场次。</p>
        <p class="page-hint">
          场次是所有数据的容器：选手、代码回收、文件下发、成绩都按场次隔离。
          先创建一个，再导入选手。
        </p>
        <el-button type="primary" style="margin-top: 12px" @click="createVisible = true">
          新建第一个场次
        </el-button>
      </div>
    </el-card>

    <ContestCreateDialog v-model="createVisible" :existing="contest.contests" @created="handleCreated" />
    <ProblemManageDialog
      v-model="problemVisible"
      :contest-id="problemContestId"
      @changed="refresh"
    />
  </div>
</template>

<style scoped>
.online-text {
  color: #67c23a;
  font-weight: 600;
}
</style>

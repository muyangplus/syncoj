<script setup lang="ts">
import { useRouter } from 'vue-router'

import { contestApi } from '@/api'
import type { ContestOut } from '@/api/types'
import DataTable from '@/components/DataTable.vue'
import PageShell from '@/components/PageShell.vue'
import SectionCard from '@/components/SectionCard.vue'
import { useList } from '@/composables/useList'
import { useMutation } from '@/composables/useMutation'
import { useContestStore } from '@/stores/contest'
import { formatTime } from '@/utils/format'

/**
 * 已归档的场次。数据都在：切过去就能看选手、代码台账、成绩，并照常导出；
 * 「取消归档」把它放回「已结束」。
 */
const contest = useContestStore()
const router = useRouter()

const list = useList<ContestOut>({
  rowKey: (row) => row.id,
  loader: ({ limit, offset, signal }) => contestApi.list({ limit, offset, archived: true }, signal),
})

async function refreshAll(): Promise<void> {
  await list.reload()
  await contest.load().catch(() => undefined)
}

/** 切到这一场再去对应页面（代码台账与成绩页各有导出）。 */
function open(row: ContestOut, name: 'overview' | 'files' | 'scores'): void {
  contest.select(row.id)
  void router.push({ name })
}

const unarchive = useMutation((row: ContestOut) => contestApi.unarchive(row.id), {
  success: (result) => `「${result.name}」已回到已结束`,
  onDone: refreshAll,
})
</script>

<template>
  <PageShell title="已归档场次" :error="list.error.value" retryable @retry="refreshAll">
    <template #toolbar>
      <el-button :loading="list.loading.value" @click="refreshAll">
        <el-icon><Refresh /></el-icon>刷新
      </el-button>
    </template>

    <SectionCard title="已归档" :meta="`${list.total.value ?? 0} 场`" flush>
      <DataTable
        :rows="list.rows.value"
        :row-key="(row: ContestOut) => row.id"
        :loading="list.loading.value"
        :total="list.total.value"
        :page="list.page.value"
        :page-size="list.pageSize.value"
        empty-text="无已归档场次"
        @update:page="list.setPage"
        @update:pageSize="list.setPageSize"
      >
        <template #columns>
          <el-table-column label="名称" min-width="200">
            <template #default="{ row }">
              <div>{{ row.name }}</div>
              <div class="cell-sub">
                标识 <span class="mono">{{ row.slug }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="选手" width="90" align="right">
            <template #default="{ row }">{{ row.player_count }} 名</template>
          </el-table-column>
          <el-table-column label="默认名单" width="140">
            <template #default="{ row }">
              <span v-if="row.default_roster_name">{{ row.default_roster_name }}</span>
              <span v-else class="muted">未指定</span>
            </template>
          </el-table-column>
          <el-table-column label="创建时间" width="170">
            <template #default="{ row }">
              <span class="cell-sub">{{ formatTime(row.created_at) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="300" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="open(row, 'overview')">选手</el-button>
              <el-button link type="primary" size="small" @click="open(row, 'files')">代码 / 导出</el-button>
              <el-button link type="primary" size="small" @click="open(row, 'scores')">成绩 / 导出</el-button>
              <el-button
                link
                type="primary"
                size="small"
                :loading="unarchive.pending.value"
                @click="unarchive.run(row)"
              >
                取消归档
              </el-button>
            </template>
          </el-table-column>
        </template>
      </DataTable>
    </SectionCard>
  </PageShell>
</template>

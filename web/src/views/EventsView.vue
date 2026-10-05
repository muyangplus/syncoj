<script setup lang="ts">
import { computed, ref } from 'vue'
import { storeToRefs } from 'pinia'

import { eventApi } from '@/api'
import type { EventOut } from '@/api/types'
import { usePolling } from '@/composables/usePolling'
import { useContestStore } from '@/stores/contest'
import { eventCategoryLabel, eventLevelLabel, formatTime } from '@/utils/format'

const contest = useContestStore()
const { currentId } = storeToRefs(contest)

const events = ref<EventOut[]>([])
const loading = ref(false)
const error = ref<string | null>(null)

const category = ref<string>('')
const onlyProblems = ref(false)

const CATEGORIES = [
  'file_deleted',
  'file_restored',
  'bulk_rewrite',
  'scan_rejected',
  'scan_incomplete',
  'offline',
  'enroll',
  'deploy_failed',
  'judge_manual',
  'release_rollout',
]

const rows = computed(() => {
  if (!onlyProblems.value) return events.value
  return events.value.filter((e) => e.level !== 'info')
})

async function refresh(): Promise<void> {
  if (!currentId.value) {
    events.value = []
    return
  }
  try {
    events.value = await eventApi.list(currentId.value, {
      limit: 500,
      category: category.value || undefined,
    })
    error.value = null
  } catch (err) {
    error.value = (err as Error).message
  } finally {
    loading.value = false
  }
}

usePolling(refresh, { interval: 8000 })

function levelType(level: string): 'info' | 'warning' | 'danger' {
  if (level === 'error') return 'danger'
  if (level === 'warning') return 'warning'
  return 'info'
}

function metaText(meta: EventOut['meta']): string {
  if (!meta) return ''
  try {
    return JSON.stringify(meta, null, 2)
  } catch {
    return String(meta)
  }
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h2 class="page-title">审计日志</h2>
        <p class="page-hint">
          只增不改。在"仅完整性校验 + 审计"这一档防作弊下，"选手删了哪个文件、
          什么时候删的、什么时候又出现的"是唯一的依据。
        </p>
      </div>
      <div class="toolbar">
        <el-select
          v-model="category"
          size="small"
          placeholder="全部分类"
          clearable
          style="width: 170px"
          @change="refresh"
        >
          <el-option
            v-for="item in CATEGORIES"
            :key="item"
            :label="eventCategoryLabel(item)"
            :value="item"
          />
        </el-select>
        <el-checkbox v-model="onlyProblems" size="small">只看异常</el-checkbox>
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

    <el-table :data="rows" v-loading="loading" border stripe size="small">
      <el-table-column label="时间" width="160">
        <template #default="{ row }">
          <span class="cell-sub">{{ formatTime(row.ts) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="级别" width="80">
        <template #default="{ row }">
          <el-tag :type="levelType(row.level)" size="small" effect="plain">
            {{ eventLevelLabel(row.level) }}
          </el-tag>
        </template>
      </el-table-column>

      <el-table-column label="分类" width="120">
        <template #default="{ row }">
          <el-tooltip :content="row.category">
            <span>{{ eventCategoryLabel(row.category) }}</span>
          </el-tooltip>
        </template>
      </el-table-column>

      <el-table-column label="选手" width="100">
        <template #default="{ row }">
          <span class="mono">{{ row.player_no || '—' }}</span>
        </template>
      </el-table-column>

      <el-table-column label="内容" min-width="320">
        <template #default="{ row }">
          <div>{{ row.message }}</div>
          <!-- 只有需要展开细节时才占地方，避免每条都撑高一行 -->
          <el-popover
            v-if="row.meta"
            placement="right"
            :width="520"
            trigger="click"
          >
            <template #reference>
              <el-button link type="primary" size="small">查看详情</el-button>
            </template>
            <pre class="meta-pre">{{ metaText(row.meta) }}</pre>
          </el-popover>
        </template>
      </el-table-column>

      <template #empty>
        <div class="empty-block">本场次还没有产生任何审计事件</div>
      </template>
    </el-table>
  </div>
</template>

<style scoped>
.meta-pre {
  margin: 0;
  max-height: 420px;
  overflow: auto;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>

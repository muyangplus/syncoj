<script setup lang="ts">
import { computed, ref } from 'vue'
import { storeToRefs } from 'pinia'

import { fileApi, playerApi } from '@/api'
import type { PlayerOut, SourceFileOut } from '@/api/types'
import { usePolling } from '@/composables/usePolling'
import { useContestStore } from '@/stores/contest'
import { formatBytes, formatTime } from '@/utils/format'

const contest = useContestStore()
const { currentId } = storeToRefs(contest)

const files = ref<SourceFileOut[]>([])
const players = ref<PlayerOut[]>([])
const loading = ref(false)
const error = ref<string | null>(null)

const playerFilter = ref<number | null>(null)
const includeDeleted = ref(false)
const keyword = ref('')

/** 同名文件的多个版本只在台账里保留最新一条，所以这里直接按修订号看"改过几次"。 */
const rows = computed(() => {
  const needle = keyword.value.trim().toLowerCase()
  return files.value.filter((file) => {
    if (!needle) return true
    return (
      file.rel_path.toLowerCase().includes(needle) ||
      file.player_no.toLowerCase().includes(needle)
    )
  })
})

const stats = computed(() => {
  const active = files.value.filter((f) => !f.deleted_at)
  const totalBytes = active.reduce((sum, f) => sum + f.size, 0)
  const deleted = files.value.filter((f) => f.deleted_at).length
  const playersWithFiles = new Set(active.map((f) => f.player_id)).size
  return { active: active.length, totalBytes, deleted, playersWithFiles }
})

async function refresh(): Promise<void> {
  if (!currentId.value) {
    files.value = []
    players.value = []
    return
  }
  try {
    const [fileList, playerList] = await Promise.all([
      fileApi.list(currentId.value, {
        playerId: playerFilter.value ?? undefined,
        includeDeleted: includeDeleted.value,
        limit: 2000,
      }),
      players.value.length ? Promise.resolve(players.value) : playerApi.list(currentId.value),
    ])
    files.value = fileList
    players.value = playerList
    error.value = null
  } catch (err) {
    error.value = (err as Error).message
  } finally {
    loading.value = false
  }
}

usePolling(refresh, { interval: 8000 })
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h2 class="page-title">代码台账</h2>
        <p class="page-hint">
          服务端保存的选手代码最新版本。同名文件的每一次内容变化都会让修订号 +1，
          迟到的旧版本会被拒绝，不会覆盖新版本。
        </p>
      </div>
      <div class="toolbar">
        <el-select
          v-model="playerFilter"
          size="small"
          placeholder="全部选手"
          clearable
          style="width: 150px"
          @change="refresh"
        >
          <el-option
            v-for="player in players"
            :key="player.id"
            :label="`${player.player_no}${player.name ? ' ' + player.name : ''}`"
            :value="player.id"
          />
        </el-select>
        <el-checkbox v-model="includeDeleted" size="small" @change="refresh">
          含已删除
        </el-checkbox>
        <el-input
          v-model="keyword"
          size="small"
          placeholder="搜索路径或选手"
          clearable
          style="width: 190px"
        />
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

    <el-row :gutter="12">
      <el-col :span="6">
        <el-card shadow="never">
          <div class="stat-value">{{ stats.active }}</div>
          <div class="stat-label">当前文件</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never">
          <div class="stat-value">{{ stats.playersWithFiles }}</div>
          <div class="stat-label">有代码的选手</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never">
          <div class="stat-value">{{ formatBytes(stats.totalBytes) }}</div>
          <div class="stat-label">占用（去重前）</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never">
          <div class="stat-value" :class="{ warn: stats.deleted > 0 }">{{ stats.deleted }}</div>
          <div class="stat-label">已消失</div>
        </el-card>
      </el-col>
    </el-row>

    <el-table
      :data="rows"
      v-loading="loading"
      border
      stripe
      size="small"
      style="margin-top: 12px"
    >
      <el-table-column label="选手" width="110" prop="player_no" />

      <el-table-column label="路径" min-width="260">
        <template #default="{ row }">
          <span class="mono" :class="{ deleted: row.deleted_at }">{{ row.rel_path }}</span>
        </template>
      </el-table-column>

      <el-table-column label="大小" width="90" align="right">
        <template #default="{ row }">{{ formatBytes(row.size) }}</template>
      </el-table-column>

      <el-table-column label="修订" width="70" align="right">
        <template #default="{ row }">
          <el-tooltip content="内容变化次数。修订号是丢弃迟到旧版本的依据。">
            <span>#{{ row.revision }}</span>
          </el-tooltip>
        </template>
      </el-table-column>

      <el-table-column label="内容哈希" width="130">
        <template #default="{ row }">
          <el-tooltip :content="row.sha256">
            <span class="mono muted">{{ row.sha256.slice(0, 12) }}…</span>
          </el-tooltip>
        </template>
      </el-table-column>

      <el-table-column label="首次回收" width="150">
        <template #default="{ row }">
          <span class="cell-sub">{{ formatTime(row.first_seen_at) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="最近出现" width="150">
        <template #default="{ row }">
          <span class="cell-sub">{{ formatTime(row.last_seen_at) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag v-if="row.deleted_at" type="danger" size="small" effect="plain">
            已消失
          </el-tag>
          <el-tag v-else-if="row.content_stored" type="success" size="small" effect="plain">
            已归档
          </el-tag>
          <!-- 有台账记录但内容不在：说明只上报了索引，内容从未成功上传 -->
          <el-tag v-else type="warning" size="small" effect="plain">缺内容</el-tag>
        </template>
      </el-table-column>

      <template #empty>
        <div class="empty-block">还没有回收任何代码</div>
      </template>
    </el-table>

    <p v-if="rows.length >= 2000" class="page-hint" style="margin-top: 8px">
      仅显示前 2000 条。用选手筛选或搜索缩小范围。
    </p>
  </div>
</template>

<style scoped>
.stat-value {
  font-size: 20px;
  font-weight: 600;
  line-height: 1.2;
}

.stat-value.warn {
  color: #e6a23c;
}

.stat-label {
  font-size: 12px;
  color: #909399;
  margin-top: 2px;
}

.mono.deleted {
  color: #c0c4cc;
  text-decoration: line-through;
}
</style>

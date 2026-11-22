<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { useRouter } from 'vue-router'

import { fileApi, playerApi } from '@/api'
import type { PlayerOut, SourceFileOut } from '@/api/types'
import ConfirmByNameDialog from '@/components/ConfirmByNameDialog.vue'
import DataTable from '@/components/DataTable.vue'
import PageShell from '@/components/PageShell.vue'
import SectionCard from '@/components/SectionCard.vue'
import StatCard from '@/components/StatCard.vue'
import { useList } from '@/composables/useList'
import { useMutation } from '@/composables/useMutation'
import { useContestStore } from '@/stores/contest'
import { formatBytes, formatTime } from '@/utils/format'

const contest = useContestStore()
const router = useRouter()

const playerFilter = ref<number | null>(null)
const includeDeleted = ref(false)
const keyword = ref('')
/** 归题筛选：``'all'`` / ``'matched'`` / ``'loose'`` / 具体题目标识。 */
const problemFilter = ref('all')

const list = useList<SourceFileOut>({
  rowKey: (row) => row.id,
  loader: ({ limit, offset, signal }) => {
    const contestId = contest.currentId
    // 还没选场次时返回 null —— 列表空着但**不报错**。"没选场次"不是错误
    if (!contestId) return Promise.resolve(null)
    return fileApi.list(
      contestId,
      {
        limit,
        offset,
        playerId: playerFilter.value ?? undefined,
        includeDeleted: includeDeleted.value,
      },
      signal,
    )
  },
  // 8 秒一轮：考试进行中教师会盯着这一页看"谁交上来了"
  interval: 8000,
  watches: [() => contest.currentId, () => playerFilter.value, () => includeDeleted.value],
})

/** 选手列表只用来做筛选下拉。它很小，一次取完。 */
const players = ref<PlayerOut[]>([])

async function loadPlayers(): Promise<void> {
  const contestId = contest.currentId
  if (!contestId) {
    players.value = []
    return
  }
  try {
    const page = await playerApi.list(contestId, { limit: 500 })
    players.value = page.items
  } catch {
    // 筛选项拉不到不该把主列表也弄成错误状态：主列表自己会报它的问题
    players.value = []
  }
}

void loadPlayers()
// 场次变了要重新取筛选下拉
watch(() => contest.currentId, () => void loadPlayers())

/** 复制正文用的小工具。只服务于这个页面的对话框，不值得单独成模块。 */
async function copyText(text: string): Promise<void> {
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch {
    // 非 HTTPS 或浏览器策略下剪贴板不可用，提示手工复制而不是静默失败
    ElMessage.warning('复制失败，请手工选取')
  }
}

/**
 * 出现过的题目归属（含"未归类"）。
 *
 * 归属是服务端按当前模式算出来的 —— 改模式立刻变，不需要重收文件。
 */
const problems = computed(() => {
  const seen = new Set<string>()
  let loose = false
  for (const file of list.rows.value) {
    if (file.problem) seen.add(file.problem)
    else loose = true
  }
  return { matched: [...seen].sort(), loose }
})

/**
 * 本页的筛选与统计。
 *
 * 归题与关键词都是**本页内**的过滤：服务端的列表接口只认 `player_id` 与
 * `include_deleted`，没有关键词参数。界面把这件事写出来，而不是假装它筛了全部 ——
 * 假装会让人以为"搜不到就是没有"。
 */
/**
 * 收卷后的默认排序：最近更新在前。
 *
 * 服务端按「选手 + 路径」出数据，那个顺序适合核对「谁交了哪几题」，
 * 但收卷时教师盯的是「谁还在改」 —— 时间戳最近的那些才是要看的。
 */
const sortMode = ref<'recent' | 'path'>('recent')

const rows = computed(() => {
  const needle = keyword.value.trim().toLowerCase()
  const matched = list.rows.value.filter((file) => {
    // 归题筛选：``未归类`` 单独一档。回收时已经按题目预设过滤过，所以这一档剩下的
    // 是两种历史记录："收上来的时候还没配题目" 与 "模式后来被改过"
    if (problemFilter.value === 'matched' && !file.problem) return false
    if (problemFilter.value === 'loose' && file.problem) return false
    if (
      problemFilter.value !== 'all' &&
      problemFilter.value !== 'matched' &&
      problemFilter.value !== 'loose' &&
      file.problem !== problemFilter.value
    ) {
      return false
    }
    if (!needle) return true
    return (
      file.rel_path.toLowerCase().includes(needle) ||
      file.player_no.toLowerCase().includes(needle)
    )
  })
  // 排序只作用在**本页**：列表接口没有排序参数。控件是显式的，
  // 所以教师看得见自己正在按什么排，而不是以为系统替他挑过一遍。
  if (sortMode.value === 'path') return matched
  return [...matched].sort((a, b) => b.last_seen_at.localeCompare(a.last_seen_at))
})

/** 本页字节合计。**精确值** —— 核对代码时"1.2 KB"答不了"是不是这一份"。 */
const pageBytes = computed(() => rows.value.reduce((sum, file) => sum + file.size, 0))
const looseCount = computed(() => rows.value.filter((file) => !file.problem).length)
const filtered = computed(() => rows.value.length !== list.rows.value.length)

// --------------------------------------------------------------------------- //
// 看一份代码
//
// "收代码的系统收完之后教师只能去磁盘上翻 source/" 是最刺眼的缺口。
// 这里让他一次点击就看到内容 —— 不落地到下载目录，也不依赖服务端的
// Content-Disposition。
// --------------------------------------------------------------------------- //

const viewerOpen = ref(false)
const viewerTitle = ref('')
const viewerText = ref('')
const viewerLoading = ref(false)
const viewerError = ref<string | null>(null)

/** 超过这个大小就不往界面上贴了：几 MB 的代码贴进 <pre> 会把浏览器卡住。 */
const VIEW_LIMIT_BYTES = 512 * 1024

async function view(row: SourceFileOut): Promise<void> {
  viewerOpen.value = true
  viewerTitle.value = `${row.player_no} · ${row.rel_path}`
  viewerText.value = ''
  viewerError.value = null

  if (row.size > VIEW_LIMIT_BYTES) {
    viewerError.value = `文件过大（${row.size} 字节），超过 ${formatBytes(VIEW_LIMIT_BYTES)} 预览上限`
    return
  }
  if (!row.content_stored) {
    viewerError.value = '缺内容：未完成上传'
    return
  }

  viewerLoading.value = true
  try {
    viewerText.value = await fileApi.content(row.id)
  } catch (err) {
    viewerError.value = (err as Error).message
  } finally {
    viewerLoading.value = false
  }
}

const downloadFile = useMutation(async (row: SourceFileOut) => {
  const name = row.rel_path.split('/').pop() || 'code.txt'
  await fileApi.download(row.id, name)
  return { detail: `已下载 ${name}` }
}, {})

// --------------------------------------------------------------------------- //
// 删除与清空
// --------------------------------------------------------------------------- //

/** 单条删除在服务端是**硬删**：行没了，勾上`含已删除`也看不到（ADR-0003 的 2026-11 修订）。 */
const removeFile = useMutation((row: SourceFileOut) => fileApi.remove(row.id), {
  onDone: () => list.reload(),
})

async function askRemove(row: SourceFileOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `将删除 ${row.player_no} 的 ${row.rel_path} 台账记录，不可恢复；机器上仍有该文件时，下一轮扫描会重新回收。`,
      '删除台账记录？',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await removeFile.run(row)
}

const clearOpen = ref(false)
const clearPurge = ref(false)

const clearFiles = useMutation(
  () => {
    const contestId = contest.currentId
    if (!contestId) throw new Error('未选择场次')
    return fileApi.clear(contestId, contest.current?.slug ?? '', {
      purge: clearPurge.value,
      playerId: playerFilter.value ?? undefined,
    })
  },
  {
    onDone: async () => {
      clearOpen.value = false
      clearPurge.value = false
      await list.reload()
    },
  },
)

// --------------------------------------------------------------------------- //
// 导出
// --------------------------------------------------------------------------- //

const exporting = ref(false)

async function exportZip(): Promise<void> {
  const contestId = contest.currentId
  if (!contestId) return
  exporting.value = true
  try {
    // 导出**默认带上已删除的代码**：考场现实是"选手确实交过，只是后来被清了"，
    // 复核成绩时这份历史比干净的数据重要
    await fileApi.exportZip(contestId, {
      playerId: playerFilter.value ?? undefined,
      problem:
        problemFilter.value === 'all' ||
        problemFilter.value === 'matched' ||
        problemFilter.value === 'loose'
          ? undefined
          : problemFilter.value,
      includeDeleted: true,
    })
    ElMessage.success('已开始导出')
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    exporting.value = false
  }
}

/** 某个筛选条件下有多少条 —— 清空确认文案里要写出真实数量，不能只写"全部"。 */
const scopeLabel = computed(() => {
  const parts: string[] = []
  const player = players.value.find((item) => item.id === playerFilter.value)
  parts.push(player ? `选手 ${player.player_no}` : '全部选手')
  if (clearPurge.value) parts.push('全部记录')
  else parts.push('已消失的记录')
  return parts.join(' · ')
})
</script>

<template>
  <PageShell title="代码台账" :error="list.error.value" retryable @retry="list.reload">
    <template #toolbar>
      <el-button :loading="list.loading.value" @click="list.reload">
        <el-icon><Refresh /></el-icon>刷新
      </el-button>
      <el-button type="primary" :loading="exporting" :disabled="!list.total.value" @click="exportZip">
        <el-icon><Download /></el-icon>导出 zip
      </el-button>
      <el-dropdown trigger="click">
        <el-button>
          更多<el-icon class="el-icon--right"><ArrowDown /></el-icon>
        </el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item @click="clearOpen = true">
              <span class="danger-item">清理台账</span>
            </el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </template>

    <div class="stats">
      <StatCard label="台账记录" :value="`${list.total.value} 条`" icon="Document" />
      <StatCard
        label="本页条数"
        :value="`${rows.length} 条`"
        :sub="filtered ? '已筛选' : undefined"
        icon="List"
        tone="info"
      />
      <StatCard
        label="本页字节"
        :value="pageBytes.toLocaleString()"
        :sub="formatBytes(pageBytes)"
        icon="Coin"
        tone="success"
      />
      <StatCard
        label="本页未归类"
        :value="`${looseCount} 个`"
        icon="Warning"
        :tone="looseCount > 0 ? 'warning' : 'info'"
      />
    </div>

    <SectionCard title="文件" :meta="`共 ${list.total.value} 条`" flush>
      <template #extra>
        <el-select
          v-model="playerFilter"
          placeholder="全部选手"
          clearable
          filterable
          style="width: 160px"
        >
          <el-option
            v-for="player in players"
            :key="player.id"
            :label="`${player.player_no}${player.name ? ' ' + player.name : ''}`"
            :value="player.id"
          />
        </el-select>
        <el-select v-model="problemFilter" style="width: 130px">
          <el-option label="全部归属" value="all" />
          <el-option label="已归题" value="matched" />
          <el-option label="未归类" value="loose" :disabled="!problems.loose" />
          <el-option v-for="ident in problems.matched" :key="ident" :label="ident" :value="ident" />
        </el-select>
        <el-select v-model="sortMode" style="width: 118px">
          <el-option label="最近更新" value="recent" />
          <el-option label="按路径" value="path" />
        </el-select>
        <el-input
          v-model="keyword"
          placeholder="本页搜索路径 / 考号"
          clearable
          style="width: 190px"
        >
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
        <el-checkbox v-model="includeDeleted">含已删除</el-checkbox>
      </template>

      <DataTable
        :rows="rows"
        :row-key="(row: SourceFileOut) => row.id"
        :loading="list.loading.value"
        :total="list.total.value"
        :page="list.page.value"
        :page-size="list.pageSize.value"
        empty-text="无代码"
        @update:page="list.setPage"
        @update:pageSize="list.setPageSize"
      >
        <template #columns>
          <el-table-column label="考号" width="110" prop="player_no" />

          <el-table-column label="路径" min-width="220">
            <template #default="{ row }">
              <span class="mono" :class="{ deleted: row.deleted_at }">{{ row.rel_path }}</span>
            </template>
          </el-table-column>

          <el-table-column label="归属" width="100">
            <template #default="{ row }">
              <el-tag v-if="row.problem" effect="plain">{{ row.problem }}</el-tag>
              <el-tag v-else type="warning" effect="plain">未归类</el-tag>
            </template>
          </el-table-column>

          <!-- 精确字节数用于核对，可读形式放 tooltip -->
          <el-table-column label="字节数" width="100" align="right">
            <template #default="{ row }">
              <el-tooltip :content="formatBytes(row.size)">
                <span class="mono">{{ row.size }}</span>
              </el-tooltip>
            </template>
          </el-table-column>

          <el-table-column label="修订" width="70" align="right">
            <template #default="{ row }">
              <span>#{{ row.revision }}</span>
            </template>
          </el-table-column>

          <el-table-column label="内容哈希" width="130">
            <template #default="{ row }">
              <el-tooltip :content="row.sha256">
                <span class="mono muted">{{ row.sha256.slice(0, 12) }}…</span>
              </el-tooltip>
            </template>
          </el-table-column>

          <el-table-column label="首次回收" width="160">
            <template #default="{ row }">
              <span class="cell-sub">{{ formatTime(row.first_seen_at) }}</span>
            </template>
          </el-table-column>

          <el-table-column label="最近出现" width="160">
            <template #default="{ row }">
              <span class="cell-sub">{{ formatTime(row.last_seen_at) }}</span>
            </template>
          </el-table-column>

          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag v-if="row.deleted_at" type="danger" effect="plain">已消失</el-tag>
              <el-tag v-else-if="row.content_stored" type="success" effect="light">已归档</el-tag>
              <el-tag v-else type="warning" effect="plain">缺内容</el-tag>
            </template>
          </el-table-column>

          <el-table-column label="操作" width="160" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="view(row)">查看</el-button>
              <el-button
                link
                type="primary"
                size="small"
                :loading="downloadFile.pending.value"
                @click="downloadFile.run(row)"
              >
                下载
              </el-button>
              <el-button link type="danger" size="small" @click="askRemove(row)">删除</el-button>
            </template>
          </el-table-column>
        </template>

        <template #empty>
          <div>无代码</div>
          <el-button link type="primary" style="margin-top: 8px" @click="router.push({ name: 'overview' })">
            查看选手状态
          </el-button>
        </template>
      </DataTable>
    </SectionCard>

    <el-dialog v-model="viewerOpen" :title="viewerTitle" width="760px">
      <el-alert v-if="viewerError" type="warning" :closable="false" show-icon :title="viewerError" />
      <div v-else v-loading="viewerLoading" class="viewer">
        <pre class="viewer-text">{{ viewerText }}</pre>
      </div>
      <template #footer>
        <el-button @click="viewerOpen = false">关闭</el-button>
        <el-button type="primary" :disabled="!viewerText" @click="copyText(viewerText)">复制全文</el-button>
      </template>
    </el-dialog>

    <!-- 清理台账：服务端硬删；confirm 为场次标识 -->
    <ConfirmByNameDialog
      v-model="clearOpen"
      title="清理代码台账？"
      :expected="contest.current?.slug ?? ''"
      :submitting="clearFiles.pending.value"
      confirm-text="清理"
      :impact="[`当前范围 ${list.total.value} 条记录`, `清理：${scopeLabel}`]"
      detail="将删除台账记录，不可恢复；不会删除机器上的文件。"
      @confirm="clearFiles.run(undefined)"
    >
      <el-checkbox v-model="clearPurge">连同未消失的记录</el-checkbox>
      <el-alert
        v-if="clearPurge"
        type="error"
        :closable="false"
        show-icon
        style="margin-top: 8px"
        title="机器上仍存在的文件会在下一轮扫描重新回收。"
      />
    </ConfirmByNameDialog>
  </PageShell>
</template>

<style scoped>
.stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 16px;
}

@media (max-width: 900px) {
  .stats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

.mono.deleted {
  color: var(--sj-text-3);
  text-decoration: line-through;
}

.danger-item {
  color: var(--sj-danger);
}

.viewer {
  max-height: 60vh;
  overflow: auto;
  background: var(--sj-surface-2);
  border: 1px solid var(--sj-border);
  border-radius: var(--sj-radius-sm);
  padding: 12px;
}

.viewer-text {
  margin: 0;
  font-family: var(--sj-mono);
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>

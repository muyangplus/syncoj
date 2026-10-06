<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { useRouter } from 'vue-router'

import { fileApi, playerApi } from '@/api'
import type { PlayerOut, SourceFileOut } from '@/api/types'
import ConfirmByNameDialog from '@/components/ConfirmByNameDialog.vue'
import DataTable from '@/components/DataTable.vue'
import HelpTip from '@/components/HelpTip.vue'
import PageShell from '@/components/PageShell.vue'
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
    ElMessage.warning('浏览器不允许自动复制，请手工选中复制')
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
    // 归题筛选：``未归类`` 单独一档，因为"模式没配上"是排错的第一步
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
    viewerError.value = `这份文件 ${formatBytes(row.size)}（${row.size} 字节），太大了，不适合在浏览器里直接看。请用「下载」。`
    return
  }
  if (!row.content_stored) {
    viewerError.value =
      '这条台账只有索引，内容从未成功上传过（Agent 上报了它，但上传没完成）。'
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

/** 单条删除是**软删除**（墓碑）：行还在，`含已删除` 勾上就能看到，导出也仍然包含。 */
const removeFile = useMutation((row: SourceFileOut) => fileApi.remove(row.id), {
  onDone: () => list.reload(),
})

async function askRemove(row: SourceFileOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `把 ${row.player_no} 的 ${row.rel_path} 从台账里删掉？\n\n` +
        '这是一条墓碑：勾上「含已删除」仍看得到；文件还在机器上，下一轮扫描会被重新收上来。',
      '删除台账记录',
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
    if (!contestId) throw new Error('还没有选场次')
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
    ElMessage.success('导出已开始，压缩包会出现在下载目录里')
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
  if (player) parts.push(`选手 ${player.player_no}`)
  if (clearPurge.value) parts.push('含未消失的记录')
  else parts.push('只清已消失的墓碑记录')
  return parts.join(' · ')
})
</script>

<template>
  <PageShell
    title="代码台账"
    hint="收上来的选手代码，同名文件只留最新版本。"
    :error="list.error.value"
    error-action="代码台账取不到时，成绩与交付判断都不可信。"
    retryable
    @retry="list.reload"
  >
    <template #hint>
      <HelpTip>
        修订号随内容变化 +1；迟到的旧版本会被服务端拒绝。
      </HelpTip>
    </template>
    <template #toolbar>
      <el-select
        v-model="playerFilter"
        size="small"
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
        <el-select v-model="sortMode" size="small" style="width: 118px">
          <el-option label="最近更新" value="recent" />
          <el-option label="按路径" value="path" />
        </el-select>
      <el-checkbox v-model="includeDeleted" size="small">含已删除</el-checkbox>
      <el-select v-model="problemFilter" size="small" style="width: 150px">
        <el-option label="全部归属" value="all" />
        <el-option label="已归题" value="matched" />
        <el-option label="未归类" value="loose" :disabled="!problems.loose" />
        <el-option v-for="ident in problems.matched" :key="ident" :label="ident" :value="ident" />
      </el-select>
      <el-input
        v-model="keyword"
        size="small"
        placeholder="本页内搜索路径或选手"
        clearable
        style="width: 200px"
      />
      <el-button size="small" :loading="list.loading.value" @click="list.reload">刷新</el-button>
      <el-button
        size="small"
        type="primary"
        :loading="exporting"
        :disabled="!list.total.value"
        @click="exportZip"
      >
        导出归档 zip
      </el-button>
      <el-button size="small" type="danger" plain @click="clearOpen = true">清理台账</el-button>
    </template>

    <el-row :gutter="12">
      <el-col :span="6">
        <el-card shadow="never">
          <div class="stat-value">{{ list.total.value }}</div>
          <div class="stat-label">台账记录（全部）</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never">
          <div class="stat-value">{{ rows.length }}</div>
          <div class="stat-label">{{ filtered ? '本页筛选后条数' : '本页条数' }}</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never">
          <!-- 精确字节数放在卡片上：教师核对"是不是收全了"时看的就是它 -->
          <el-tooltip :content="`约 ${formatBytes(pageBytes)}`">
            <div class="stat-value mono">{{ pageBytes.toLocaleString() }}</div>
          </el-tooltip>
          <div class="stat-label">本页字节合计</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never">
          <div class="stat-value" :class="{ warn: looseCount > 0 }">{{ looseCount }}</div>
          <div class="stat-label">本页未归类</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 归题情况单独说清楚：它只影响"算哪道题"，不影响文件本身收没收上来 -->
    <div class="problem-summary">
      <el-tag v-if="problems.matched.length" size="small" type="info" effect="plain">
        本页已归题：{{ problems.matched.join('　') }}
      </el-tag>
      <el-tag v-if="problems.loose" size="small" type="warning" effect="plain">
        本页有文件没归到任何题目
      </el-tag>
      <span class="muted">「归属」由题目清单里的「代码路径」模式决定，改模式立刻生效。</span>
    </div>

    <DataTable
      :rows="rows"
      :row-key="(row: SourceFileOut) => row.id"
      :loading="list.loading.value"
      :total="list.total.value"
      :page="list.page.value"
      :page-size="list.pageSize.value"
      empty-text="还没有回收任何代码"
      @update:page="list.setPage"
      @update:pageSize="list.setPageSize"
    >
      <template #columns>
        <el-table-column label="选手" width="110" prop="player_no" />

        <el-table-column label="路径" min-width="220">
          <template #default="{ row }">
            <span class="mono" :class="{ deleted: row.deleted_at }">{{ row.rel_path }}</span>
          </template>
        </el-table-column>

        <el-table-column label="归属" width="100">
          <template #default="{ row }">
            <el-tag v-if="row.problem" size="small" effect="plain">{{ row.problem }}</el-tag>
            <el-tooltip
              v-else
              content="没有题目认领这条路径。文件收上来了，只是不算到某道题头上。"
            >
              <el-tag size="small" type="warning" effect="plain">未归类</el-tag>
            </el-tooltip>
          </template>
        </el-table-column>

        <!--
          **精确字节数**，不是"1.2 KB"。

          这一列存在的意义就是核对："我手上这份 main.cpp 是 1043 字节，
          台账里这条也是 1043，那就是它。" 而"1.2 KB"在 1043 与 1180 之间
          给不出任何区别 —— 而那正是传坏、传成旧版本时的差别。
          人类可读的形式放到 tooltip 里，它是辅助，不是判据。
        -->
        <el-table-column label="字节数" width="100" align="right">
          <template #default="{ row }">
            <el-tooltip :content="formatBytes(row.size)">
              <span class="mono">{{ row.size }}</span>
            </el-tooltip>
          </template>
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

        <el-table-column label="操作" width="150" fixed="right">
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
        <p>还没有回收任何代码。</p>
        <p class="page-hint">一直空着说明机器没在线或没配对。</p>
        <el-button type="primary" size="small" style="margin-top: 12px" @click="router.push({ name: 'overview' })">
          去看选手状态
        </el-button>
      </template>
    </DataTable>

    <p v-if="list.total.value > rows.length" class="page-hint" style="margin-top: 8px">
      搜索与归题筛选只作用在本页 —— 要缩小范围请用选手筛选。
    </p>

    <!-- 看代码正文 -->
    <el-dialog v-model="viewerOpen" :title="viewerTitle" width="760px">
      <el-alert
        v-if="viewerError"
        type="warning"
        :closable="false"
        show-icon
        :title="viewerError"
      />
      <div v-loading="viewerLoading" class="viewer">
        <pre class="viewer-text">{{ viewerText }}</pre>
      </div>
      <template #footer>
        <el-button @click="viewerOpen = false">关闭</el-button>
        <el-button type="primary" @click="copyText(viewerText)">复制全文</el-button>
      </template>
    </el-dialog>

    <!--
      清理台账 = 范围删除。代码是产物性数据，所以这里是软删除；但"连没消失的
      一起删"是另一档风险（机器还在报的文件下一轮就会回来），要显式勾选。
      `confirm` 是场次标识，服务端会逐字校验。
    -->
    <ConfirmByNameDialog
      v-model="clearOpen"
      title="清理代码台账"
      :expected="contest.current?.slug ?? ''"
      :submitting="clearFiles.pending.value"
      confirm-text="清理"
      :impact="[
        `当前范围共 ${list.total.value} 条记录`,
        clearPurge ? '连还在的记录一起清理（已勾选）' : '只清「已消失」的墓碑记录',
      ]"
      :detail="`将清理：${scopeLabel}。`"
      @confirm="clearFiles.run(undefined)"
    >
      <div style="margin-bottom: 4px">
        <el-checkbox v-model="clearPurge">
          连未消失的记录一起清理（比赛结束后清场用）
        </el-checkbox>
      </div>
      <el-alert v-if="clearPurge" type="error" :closable="false" show-icon>
        <template #title>注意：机器还在报的文件下一轮就会被重新收上来</template>
        <template #default>
          它不会删掉学生机器上的文件。
        </template>
      </el-alert>
    </ConfirmByNameDialog>
  </PageShell>
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

.problem-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-top: 12px;
}

.muted {
  color: #909399;
  font-size: 12px;
}

.viewer {
  max-height: 60vh;
  overflow: auto;
  background: #f5f7fa;
  border-radius: 4px;
  padding: 12px;
}

.viewer-text {
  margin: 0;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { rosterApi } from '@/api'
import type { RosterDetailOut, RosterEntryIn, RosterOut } from '@/api/types'
import { useContestStore } from '@/stores/contest'
import { parsePasteLines } from '@/utils/paste'

const contest = useContestStore()

const rosters = ref<RosterOut[]>([])
const loading = ref(false)
const selectedId = ref<number | null>(null)
const detail = ref<RosterDetailOut | null>(null)
const detailLoading = ref(false)

/**
 * 名单库是**全局**的，不跟着当前场次走 —— 它的全部价值就是跨场次复用。
 * 所以这里不用 useContestData，自己管加载。
 */
async function loadRosters(): Promise<void> {
  loading.value = true
  try {
    rosters.value = await rosterApi.list()
    if (selectedId.value === null && rosters.value.length) {
      selectedId.value = rosters.value[0].id
    }
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    loading.value = false
  }
}

async function loadDetail(): Promise<void> {
  if (selectedId.value === null) {
    detail.value = null
    return
  }
  detailLoading.value = true
  try {
    detail.value = await rosterApi.get(selectedId.value)
  } catch (err) {
    ElMessage.error((err as Error).message)
    detail.value = null
  } finally {
    detailLoading.value = false
  }
}

watch(selectedId, () => void loadDetail())

void loadRosters()

// --------------------------------------------------------------------------- //
// 增删改
// --------------------------------------------------------------------------- //

async function createRoster(): Promise<void> {
  let name: string
  try {
    const result = await ElMessageBox.prompt('名单名称，比如「高一(1)班」', '新建名单', {
      confirmButtonText: '创建',
      cancelButtonText: '取消',
      inputValidator: (value) => (value?.trim() ? true : '名称不能为空'),
    })
    name = result.value.trim()
  } catch {
    return
  }

  try {
    const roster = await rosterApi.create({ name, note: null })
    ElMessage.success('已创建')
    await loadRosters()
    selectedId.value = roster.id
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

async function renameRoster(): Promise<void> {
  const current = selectedRoster.value
  if (!current) return

  let name: string
  try {
    const result = await ElMessageBox.prompt('新的名单名称', '重命名', {
      confirmButtonText: '保存',
      cancelButtonText: '取消',
      inputValue: current.name,
      inputValidator: (value) => (value?.trim() ? true : '名称不能为空'),
    })
    name = result.value.trim()
  } catch {
    return
  }

  try {
    await rosterApi.update(current.id, { name, note: current.note ?? null })
    ElMessage.success('已保存')
    await loadRosters()
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

const selectedRoster = computed(
  () => rosters.value.find((roster) => roster.id === selectedId.value) ?? null,
)

async function removeRoster(): Promise<void> {
  const current = selectedRoster.value
  if (!current) return

  try {
    await ElMessageBox.confirm(
      `删除名单「${current.name}」？\n\n` +
        '已经应用到场次的选手不受影响 —— 名单是模板，场次是从它复制出去的。' +
        '删除只是以后没得选了。',
      '删除名单',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }

  try {
    const ack = await rosterApi.remove(current.id)
    ElMessage.success(ack.detail ?? '已删除')
    selectedId.value = null
    await loadRosters()
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

async function removeEntry(entryId: number, playerNo: string): Promise<void> {
  try {
    await rosterApi.removeEntry(entryId)
    ElMessage.success(`已移除 ${playerNo}`)
    await Promise.all([loadRosters(), loadDetail()])
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

// --------------------------------------------------------------------------- //
// 粘贴导入
// --------------------------------------------------------------------------- //

const importVisible = ref(false)
const importing = ref(false)
const raw = ref('')

interface ParsedRow {
  line: number
  player_no: string
  name: string
  seat: string
  group_name: string
  error?: string
}

const parsed = computed<ParsedRow[]>(() => {
  const seen = new Set<string>()
  const rows: ParsedRow[] = []

  for (const { line, columns } of parsePasteLines(raw.value)) {
    const [playerNo = '', name = '', seat = '', group = ''] = columns
    const row: ParsedRow = {
      line,
      player_no: playerNo,
      name,
      seat,
      group_name: group,
    }

    if (!playerNo) {
      row.error = '缺少选手编号'
    } else if (playerNo.length > 64) {
      row.error = '编号超过 64 字符'
    } else if (name.length > 64) {
      row.error = '姓名超过 64 字符'
    } else if (seat.length > 32) {
      row.error = '座位超过 32 字符'
    } else if (group.length > 64) {
      row.error = '分组超过 64 字符'
    } else if (seen.has(playerNo)) {
      row.error = '本次粘贴中重复'
    } else {
      seen.add(playerNo)
    }
    rows.push(row)
  }

  return rows
})

const validRows = computed(() => parsed.value.filter((row) => !row.error))
const invalidRows = computed(() => parsed.value.filter((row) => row.error))

function openImport(): void {
  raw.value = ''
  importVisible.value = true
}

async function submitImport(): Promise<void> {
  if (selectedId.value === null || !validRows.value.length) return
  importing.value = true
  try {
    const payload: RosterEntryIn[] = validRows.value.map((row) => ({
      player_no: row.player_no,
      name: row.name || null,
      seat: row.seat || null,
      group_name: row.group_name || null,
    }))
    const result = await rosterApi.importEntries(selectedId.value, payload)
    if (result.errors?.length) {
      ElMessage.warning(
        `新增 ${result.created} 条，更新 ${result.updated} 条，跳过 ${result.skipped} 条`,
      )
      for (const message of result.errors.slice(0, 5)) ElMessage.error(message)
    } else {
      ElMessage.success(`新增 ${result.created} 条，更新 ${result.updated} 条`)
    }
    importVisible.value = false
    await Promise.all([loadRosters(), loadDetail()])
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    importing.value = false
  }
}

// --------------------------------------------------------------------------- //
// 应用到场次
// --------------------------------------------------------------------------- //

const applying = ref(false)

/**
 * 把名单应用到场次。
 *
 * **默认不删人**，而且即使用户勾了"清理"，服务端也不会删已经有代码或成绩的
 * 选手 —— 名单是用来补人的，不是用来清场的。
 */
async function applyToContest(prune: boolean): Promise<void> {
  const current = selectedRoster.value
  if (!current) return
  if (!contest.currentId) {
    ElMessage.warning('请先在顶栏选择一个场次')
    return
  }

  const target = contest.current
  const confirmText = prune
    ? `把名单「${current.name}」应用到「${target?.name ?? ''}」？\n\n` +
      '名单里没有的选手会被移除，**但已经交过代码或有成绩的会被保留**。'
    : `把名单「${current.name}」应用到「${target?.name ?? ''}」？\n\n` +
      '名单里有的补上、姓名座位更新，场次里多出来的选手保持不动。'

  try {
    await ElMessageBox.confirm(confirmText, '应用名单', {
      type: prune ? 'warning' : 'info',
      confirmButtonText: '应用',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }

  applying.value = true
  try {
    const report = await rosterApi.applyToContest(contest.currentId, {
      roster_id: current.id,
      prune,
    })
    const parts = [`新增 ${report.created}`, `更新 ${report.updated}`, `保留 ${report.kept}`]
    if (report.pruned) parts.push(`移除 ${report.pruned}`)
    ElMessage.success(parts.join('，'))
    if (report.protected?.length) {
      ElMessage.warning(
        `以下选手已经交过代码或有成绩，没有被移除：${report.protected.join('、')}`,
      )
    }
    await contest.load()
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    applying.value = false
  }
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h2 class="page-title">名单库</h2>
        <p class="page-hint">
          一次录入、多场次复用。<strong>名单是模板</strong>，场次里的选手是从它
          <strong>复制</strong>出去的独立数据 —— 改名单不会影响已经应用过的场次，
          要显式点「应用」。
        </p>
      </div>
      <div class="toolbar">
        <el-button size="small" :loading="loading" @click="loadRosters">刷新</el-button>
        <el-button size="small" type="primary" @click="createRoster">新建名单</el-button>
      </div>
    </div>

    <el-card shadow="never" style="margin-bottom: 12px">
      <div class="picker">
        <el-select
          v-model="selectedId"
          placeholder="选择名单"
          style="width: 260px"
          :loading="loading"
        >
          <el-option
            v-for="roster in rosters"
            :key="roster.id"
            :label="`${roster.name}（${roster.entry_count} 人）`"
            :value="roster.id"
          />
        </el-select>

        <template v-if="selectedRoster">
          <el-button size="small" @click="renameRoster">重命名</el-button>
          <el-button size="small" type="danger" plain @click="removeRoster">删除</el-button>
          <div class="spacer" />
          <el-button size="small" type="primary" :disabled="!detail?.entries?.length" @click="openImport">
            粘贴导入
          </el-button>
          <el-button
            size="small"
            type="success"
            plain
            :loading="applying"
            :disabled="!contest.currentId || !detail?.entries?.length"
            @click="applyToContest(false)"
          >
            应用到「{{ contest.current?.name ?? '未选场次' }}」
          </el-button>
          <el-tooltip content="名单里没有的选手会被移除；已经有代码或成绩的一律保留，并在结果里列出来。">
            <el-button
              size="small"
              plain
              :loading="applying"
              :disabled="!contest.currentId || !detail?.entries?.length"
              @click="applyToContest(true)"
            >
              应用并清理
            </el-button>
          </el-tooltip>
        </template>
      </div>
    </el-card>

    <el-table
      v-if="detail"
      :data="detail.entries ?? []"
      v-loading="detailLoading"
      border
      stripe
      size="small"
    >
      <el-table-column label="选手编号" width="140">
        <template #default="{ row }">
          <span class="mono">{{ row.player_no }}</span>
        </template>
      </el-table-column>
      <el-table-column label="姓名" width="140">
        <template #default="{ row }">{{ row.name || '—' }}</template>
      </el-table-column>
      <el-table-column label="座位" width="100">
        <template #default="{ row }">{{ row.seat || '—' }}</template>
      </el-table-column>
      <el-table-column label="分组" width="120">
        <template #default="{ row }">{{ row.group_name || '—' }}</template>
      </el-table-column>
      <el-table-column label="操作" width="90" fixed="right">
        <template #default="{ row }">
          <el-button link type="danger" size="small" @click="removeEntry(row.id, row.player_no)">
            移除
          </el-button>
        </template>
      </el-table-column>

      <template #empty>
        <div class="empty-block">
          <p>这份名单还是空的</p>
          <el-button type="primary" size="small" @click="openImport">粘贴导入</el-button>
        </div>
      </template>
    </el-table>

    <el-card v-else shadow="never">
      <div class="empty-block">
        <p>还没有名单。</p>
        <p class="page-hint">
          名单是"这个班/这个考点有哪些人"的事实。建一份之后，每场比赛都能一键应用，
          不用反复导入 Excel。
        </p>
        <el-button type="primary" style="margin-top: 12px" @click="createRoster">
          新建第一份名单
        </el-button>
      </div>
    </el-card>

    <el-dialog v-model="importVisible" title="粘贴导入名单" width="720px" :close-on-click-modal="false">
      <el-alert type="info" :closable="false" show-icon style="margin-bottom: 12px">
        <template #title>每行一名选手，用逗号、制表符或空格分隔</template>
        <template #default>
          <code>编号,姓名,座位,分组</code> —— 只有编号是必填的。
          直接从 Excel 复制粘贴即可（制表符分隔）。以 <code>#</code> 开头的行会被忽略。
          <br />
          <strong>相同编号会更新已有条目</strong>，所以名单可以反复导。
        </template>
      </el-alert>

      <el-input
        v-model="raw"
        type="textarea"
        :rows="10"
        placeholder="S001,张三,A1,A组&#10;S002,李四,A2,A组&#10;S003,王五,B1,B组"
      />

      <div v-if="parsed.length" class="preview">
        <div class="preview-head">
          <span>解析预览</span>
          <span>
            <el-tag type="success" size="small" effect="plain">
              {{ validRows.length }} 条可导入
            </el-tag>
            <el-tag
              v-if="invalidRows.length"
              type="danger"
              size="small"
              effect="plain"
              style="margin-left: 6px"
            >
              {{ invalidRows.length }} 条有问题
            </el-tag>
          </span>
        </div>

        <el-table :data="parsed" size="small" border max-height="220">
          <el-table-column label="行" prop="line" width="55" align="right" />
          <el-table-column label="编号" prop="player_no" width="110">
            <template #default="{ row }">
              <span class="mono">{{ row.player_no || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="姓名" prop="name" width="110">
            <template #default="{ row }">{{ row.name || '—' }}</template>
          </el-table-column>
          <el-table-column label="座位" prop="seat" width="90">
            <template #default="{ row }">{{ row.seat || '—' }}</template>
          </el-table-column>
          <el-table-column label="分组" prop="group_name" width="100">
            <template #default="{ row }">{{ row.group_name || '—' }}</template>
          </el-table-column>
          <el-table-column label="问题" min-width="140">
            <template #default="{ row }">
              <span v-if="row.error" class="error-text">{{ row.error }}</span>
              <span v-else class="muted">正常</span>
            </template>
          </el-table-column>
        </el-table>

        <p v-if="invalidRows.length" class="page-hint">
          有问题的行会被自动跳过，其余照常导入。
        </p>
      </div>

      <template #footer>
        <el-button @click="importVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="importing"
          :disabled="!validRows.length"
          @click="submitImport"
        >
          导入 {{ validRows.length }} 条
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.picker {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.spacer {
  flex: 1;
}

.preview {
  margin-top: 14px;
}

.preview-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 600;
}

.error-text {
  color: #f56c6c;
}
</style>

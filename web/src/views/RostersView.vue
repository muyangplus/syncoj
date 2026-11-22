<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { rosterApi } from '@/api'
import type {
  ApplyRosterOut,
  RosterDetailOut,
  RosterEntryIn,
  RosterEntryOut,
  RosterOut,
} from '@/api/types'
import ConfirmByNameDialog from '@/components/ConfirmByNameDialog.vue'
import DataTable from '@/components/DataTable.vue'
import FormDialog from '@/components/FormDialog.vue'
import PageShell from '@/components/PageShell.vue'
import RosterMatchDialog from '@/components/RosterMatchDialog.vue'
import SectionCard from '@/components/SectionCard.vue'
import { useList } from '@/composables/useList'
import { useMutation } from '@/composables/useMutation'
import { useContestStore } from '@/stores/contest'
import { parsePasteLines } from '@/utils/paste'

const contest = useContestStore()

// --------------------------------------------------------------------------- //
// 名单列表
//
// 名单库是**全局**的，不跟着当前场次走 —— 它的全部价值就是跨场次复用。
// 所以这里只按 name 排序取一页，"当前用的是哪份名单"是页面自己的状态。
// --------------------------------------------------------------------------- //

const selectedRosterId = ref<number | null>(null)

const rosters = useList<RosterOut>({
  rowKey: (row) => row.id,
  // 一次把名单库取完（它是有界的小集合）。分页会给"选中的那份不在当前页"留一个
  // 空隙：`selectedRoster` 找不到行，页面看起来像"名单不见了"—— 分数很少，
  // 不值得为此让选择状态含糊。
  pageSize: 500,
  loader: async ({ limit, offset, signal }) => {
    const page = await rosterApi.list({ limit, offset }, signal)
    // 只在"还没选"时替教师选第一份。之后每次刷新都不能覆盖他自己的选择：
    // 他去建了第二份名单、正在里面录人，一个刷新把选中项换回第一份，
    // 录进去的人就进了别人的名单 —— 这类"数据进错地方"的错最难发现
    if (selectedRosterId.value === null && page.items.length) {
      selectedRosterId.value = page.items[0].id
    }
    return page
  },
})

const selectedRoster = computed(
  () => rosters.rows.value.find((row) => row.id === selectedRosterId.value) ?? null,
)
const selectedName = computed(() => selectedRoster.value?.name ?? '')

// --------------------------------------------------------------------------- //
// 名单条目
//
// 条目是多带一个 `roster_id` 的 RosterEntryOut，所以换名单只要把
// `selectedRosterId` 交给 `watches`：useList 会回第一页重拉，不需要页面自己
// watch + reload（那正是这一轮要消掉的重复）。
// --------------------------------------------------------------------------- //

const keyword = ref('')

/** 名单详情接口一次回整份条目，所以分页在本地切 —— 一份名单上千人也不稀奇。 */
const allEntries = ref<RosterEntryOut[]>([])

const entries = useList<RosterEntryOut>({
  rowKey: (row) => row.id,
  // 一页装完：关键词筛选与本页分页叠在一起时，"筛掉了谁"就分不清了。
  // 上千人的名单才需要翻页，那时也还是能翻 —— 只是不默认把过滤结果切开。
  pageSize: 500,
  loader: async ({ limit, offset }) => {
    const rosterId = selectedRosterId.value
    // 还没选名单：返回 null —— 列表清空但**不报错**。"没选名单"不是错误，
    // 走错误通道会在页面上挂一条谁也不需要的红字
    if (rosterId === null) {
      allEntries.value = []
      return null
    }
    const detail: RosterDetailOut = await rosterApi.get(rosterId)
    allEntries.value = detail.entries ?? []
    return {
      items: allEntries.value.slice(offset, offset + limit),
      total: detail.entry_count,
      limit,
      offset,
    }
  },
  watches: [() => selectedRosterId.value],
})

/**
 * 关键词过滤。
 *
 * 服务端的名单条目接口只认 `roster_id`，没有关键词参数 —— 所以这是**取回整份
 * 名单之后**在本地筛的（和代码台账页同一个取舍）。默认每页 500 条，一份名单
 * 通常一页就装得下；真的超过一页时下面那行提示会说清"筛的是当前这一页"。
 */
const visibleEntries = computed(() => {
  const needle = keyword.value.trim().toLowerCase()
  if (!needle) return entries.rows.value
  return entries.rows.value.filter((row) =>
    [row.player_no, row.name, row.seat, row.group_name]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(needle)),
  )
})

// --------------------------------------------------------------------------- //
// 新建 / 编辑名单
//
// 用 FormDialog 而不是 `ElMessageBox.prompt`：改名和改备注是同一件事的两个字段，
// 分两个 prompt 弹会让人以为"备注要单独存一次"，而它其实是同一次 PATCH。
// 备注也值得写 —— 名单库里有好几份「高一(1)班」时，靠它才分得清哪个是哪个。
// --------------------------------------------------------------------------- //

const formOpen = ref(false)
const formTitle = ref('新建名单')
/**
 * 正在编辑的那份名单，`null` 表示"这是新建"。
 *
 * 用 `ref` 而不是 `computed(() => formOpen && editing)`：表单要先写字段再开弹窗，
 * 而那种写法会在字段赋值的中间态就把弹窗打开一次。
 */
const editingRoster = ref<RosterOut | null>(null)
const formName = ref('')
const formNote = ref('')

function openCreate(): void {
  editingRoster.value = null
  formName.value = ''
  formNote.value = ''
  formTitle.value = '新建名单'
  formOpen.value = true
}

function openEdit(roster: RosterOut): void {
  editingRoster.value = roster
  formName.value = roster.name
  formNote.value = roster.note ?? ''
  formTitle.value = `编辑名单「${roster.name}」`
  formOpen.value = true
}

const saveRoster = useMutation(
  () => {
    const name = formName.value.trim()
    if (!name) throw new Error('名单名称不能为空')
    const note = formNote.value.trim() || null
    const current = editingRoster.value
    if (current) return rosterApi.update(current.id, { name, note })
    return rosterApi.create({ name, note })
  },
  {
    // `RosterOut` 没有 `detail` 字段（它不是 SimpleAck），所以这里自己写一句。
    // 重点是"改名/改备注都不会动任何场次里的选手"—— 这句话不说，教师不敢改。
    success: () => (editingRoster.value ? '已保存' : '已新建名单'),
    onDone: async (roster) => {
      formOpen.value = false
      // 新建的那份立刻选中：接下来一定是往里面导入人
      if (!editingRoster.value) selectedRosterId.value = roster.id
      await rosters.reload()
    },
  },
)

// --------------------------------------------------------------------------- //
// 删除名单
//
// 名单是结构性数据 —— 硬删除必须打一遍名字（`ConfirmByNameDialog`）。
// 弹窗里那句"什么不会被删"是这个按钮敢按下去的全部理由：教师最怕的是
// "删了名单，某个场次的选手跟着没了"，而实际不会。
// --------------------------------------------------------------------------- //

const deleteOpen = ref(false)
const deleteTarget = ref<RosterOut | null>(null)

const removeRoster = useMutation(
  () => {
    const roster = deleteTarget.value
    if (!roster) throw new Error('没有选中名单')
    return rosterApi.remove(roster.id, roster.name)
  },
  {
    onDone: async () => {
      deleteOpen.value = false
      deleteTarget.value = null
      // 选中项先置空的时机很关键：rosterApi.remove 之后 reload 回来的第一份名单
      // 会被 loader 自动选中，于是页面换到那份名单上，而不是停在一个已消失的 id
      selectedRosterId.value = null
      await rosters.reload()
    },
  },
)

function askRemoveRoster(): void {
  const roster = selectedRoster.value
  if (!roster) return
  deleteTarget.value = roster
  deleteOpen.value = true
}

/**
 * 删除确认里那句"什么不会被删"。
 *
 * 这句是这个按钮敢按下去的全部理由：教师最怕的是"删了名单，某场比赛的选手跟着
 * 没了"。不过度用强调符号 —— 弹窗是纯文本，`**` 只会原样显示出来。
 */
const deleteRosterDetail = computed(
  () =>
    `将删除名单「${deleteTarget.value?.name ?? ''}」及其 ${deleteTarget.value?.entry_count ?? 0} 条记录，不可恢复；` +
    '各场次选手不受影响。',
)

// --------------------------------------------------------------------------- //
// 名单条目：编辑单条
//
// 以前改一个打错的考号只能"删了重加"，而删掉的瞬间这一条就没了 ——
// 编号是机器配对和代码目录的依据，改错一次的代价远大于多开一个弹窗。
// --------------------------------------------------------------------------- //

const entryOpen = ref(false)
const entryTarget = ref<RosterEntryOut | null>(null)
const entryNo = ref('')
const entryName = ref('')
const entrySeat = ref('')
const entryGroup = ref('')

function openEntry(entry: RosterEntryOut): void {
  entryTarget.value = entry
  entryNo.value = entry.player_no
  entryName.value = entry.name ?? ''
  entrySeat.value = entry.seat ?? ''
  entryGroup.value = entry.group_name ?? ''
  entryOpen.value = true
}

const saveEntry = useMutation(
  () => {
    const entry = entryTarget.value
    if (!entry) throw new Error('没有选中条目')
    const playerNo = entryNo.value.trim()
    if (!playerNo) throw new Error('考号不能为空')
    const payload: RosterEntryIn = {
      player_no: playerNo,
      name: entryName.value.trim() || null,
      seat: entrySeat.value.trim() || null,
      group_name: entryGroup.value.trim() || null,
    }
    return rosterApi.updateEntry(entry.id, payload)
  },
  {
    // 接口回的是 `RosterEntryOut`（没有 detail），编不出比"已保存"更有信息量的话，
    // 但必须提一句"已经应用过的场次不会跟着变"—— 否则教师会以为改名单等于改比赛
    success: () => '已保存',
    onDone: async () => {
      entryOpen.value = false
      entryTarget.value = null
      await Promise.all([entries.reload(), rosters.reload()])
    },
  },
)

// --------------------------------------------------------------------------- //
// 名单条目：删除
// --------------------------------------------------------------------------- //

/** 单条删除的目标。`ConfirmByNameDialog` 要打的名字就是这条的考号。 */
const entryDeleteOpen = ref(false)
const entryDeleteTarget = ref<RosterEntryOut | null>(null)

const removeEntry = useMutation(
  () => {
    const entry = entryDeleteTarget.value
    if (!entry) throw new Error('没有选中条目')
    // 逐字传考号：服务端的 `DELETE /roster-entries/{id}?confirm=<考号>` 会校验它，
    // 所以"删错一条"在提交那一刻就被拦下，不靠界面自觉
    return rosterApi.removeEntry(entry.id, entry.player_no)
  },
  {
    onDone: async () => {
      entryDeleteOpen.value = false
      entryDeleteTarget.value = null
      // 条目数变了，名单下拉里的「(N 人)」也要跟着更新，所以两个都刷
      await Promise.all([entries.reload(), rosters.reload()])
    },
  },
)

const batchRemoving = ref(false)

/**
 * 批量删除选中的条目。
 *
 * 刻意**不用 useMutation**：它的语义是"一次动作、一个回执"，而这里是 N 次动作，
 * 其中几条失败必须**汇总报出来**（"删了 8 条，2 条没删掉：S003：…"）——
 * 只弹最后一条错误会让教师以为整体成功。服务端没有批量删条目的接口，
 * 所以只能是逐条调用。
 */
async function batchRemoveEntries(): Promise<void> {
  const picked = entries.selected.value
  if (!picked.length) return
  try {
    await ElMessageBox.confirm(
      `将从名单「${selectedName.value}」删除 ${picked.length} 条记录，不可恢复；各场次选手不受影响。`,
      `删除 ${picked.length} 条？`,
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }

  batchRemoving.value = true
  const failures: string[] = []
  try {
    // 刻意串行：一次打十几条 DELETE 对本地服务端不是问题，而失败时能明确知道
    // 断在哪一条，报出来的话才有用
    for (const entry of picked) {
      try {
        // 每一条都把考号当 `confirm` 发出去 —— 批量删除在服务端也是逐条校验的，
        // 没有"一次点掉十条"的捷径
        await rosterApi.removeEntry(entry.id, entry.player_no)
      } catch (err) {
        failures.push(`${entry.player_no}：${(err as Error).message}`)
      }
    }
  } finally {
    batchRemoving.value = false
  }

  entries.clearSelection()
  await Promise.all([entries.reload(), rosters.reload()])

  const done = picked.length - failures.length
  if (failures.length) {
    ElMessage.warning(`已删除 ${done} 条，失败 ${failures.length} 条`)
    for (const message of failures.slice(0, 5)) ElMessage.error(message)
  } else {
    ElMessage.success(`已删除 ${done} 条`)
  }
}

function askRemoveEntry(entry: RosterEntryOut): void {
  // 服务端把 `confirm` 定义成**必填**，值就是这条的考号 —— 所以这里也走
  // 「把名字打一遍」那套确认，而不是"是否确定"：连续整理一份名单时，
  // 后者会被点成肌肉记忆，而把考号打一遍不会
  entryDeleteTarget.value = entry
  entryDeleteOpen.value = true
}

// --------------------------------------------------------------------------- //
// 名单条目：清空
//
// 清空是**范围删除**（`docs/reference/api-conventions.md` §5.3），所以走
// `ConfirmByNameDialog` + 名单名称，和删名单本身同一档风险。
// --------------------------------------------------------------------------- //

const clearOpen = ref(false)
/**
 * 清空的目标单独存，不复用删除名单的 `deleteTarget`。
 *
 * 两个动作的"目标"含义不同（一个要清条目、一个要删名单本身），而删除成功时
 * 会把它置空 —— 共用的话，"删完名单紧接着清空"就会拿到空目标，提交时才发现
 * 没有对象，或者更糟：清到了上一次选的那份名单上。
 */
const clearTarget = ref<RosterOut | null>(null)

function askClearEntries(): void {
  const roster = selectedRoster.value
  if (!roster) return
  clearTarget.value = roster
  clearOpen.value = true
}

const clearEntries = useMutation(
  () => {
    const roster = clearTarget.value
    if (!roster) throw new Error('没有选中名单')
    return rosterApi.clearEntries(roster.id, roster.name)
  },
  {
    onDone: async () => {
      clearOpen.value = false
      clearTarget.value = null
      await Promise.all([entries.reload(), rosters.reload()])
    },
  },
)

// --------------------------------------------------------------------------- //
// 粘贴导入
//
// 保留原有的批量录入方式：教师手上的人通常本来就是 Excel 里的一张表，
// 粘贴一行行比在界面上点十几遍"新增"快得多。
// --------------------------------------------------------------------------- //

const importVisible = ref(false)
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

    // 这里做的是"提交前的体检"，真正决定收不收的是服务端。长度上限与服务端
    // 逐条校验的一致（编号 64、姓名 64、座位 32、分组 64）
    if (!playerNo) {
      row.error = '缺少考号'
    } else if (playerNo.length > 64) {
      row.error = '考号超过 64 字符'
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

/** 导入结果里被拒的那几行要单独亮出来 —— 只说"新增 3 条"会让另外 2 行静默消失。 */
function showImportErrors(result: { errors?: string[] }): void {
  for (const message of (result.errors ?? []).slice(0, 5)) ElMessage.error(message)
}

const importEntries = useMutation(
  async () => {
    const rosterId = selectedRosterId.value
    if (rosterId === null) throw new Error('未选择名单')
    const payload: RosterEntryIn[] = validRows.value.map((row) => ({
      player_no: row.player_no,
      name: row.name || null,
      seat: row.seat || null,
      group_name: row.group_name || null,
    }))
    const result = await rosterApi.importEntries(rosterId, payload)
    const parts = [`新增 ${result.created} 条`, `更新 ${result.updated} 条`]
    if (result.skipped) parts.push(`跳过 ${result.skipped} 条`)
    // 走的仍是 success 通道，但这一句是**服务端逐条容错**的那部分结果：
    // 它已经告诉了我们哪几行被拒，不能因为整体 200 就吞掉
    return { detail: parts.join('，'), errors: result.errors ?? [] }
  },
  {
    onDone: async (result) => {
      importVisible.value = false
      raw.value = ''
      await Promise.all([entries.reload(), rosters.reload()])
      showImportErrors(result)
    },
  },
)

function openImport(): void {
  raw.value = ''
  importVisible.value = true
}

// --------------------------------------------------------------------------- //
// 应用到场次
//
// 回执必须**完整展示**：created/updated 只是"补了谁"，kept 是"我没动谁"，
// pruned 是"清了谁"，protected 是"本该清掉、但因为有代码或成绩被保住的"——
// 最后这一项是教师唯一必须看一眼的东西。
// --------------------------------------------------------------------------- //

const applying = ref(false)
const applyReport = ref<ApplyRosterOut | null>(null)
const reportOpen = ref(false)

async function applyToContest(prune: boolean): Promise<void> {
  const roster = selectedRoster.value
  if (!roster) return
  const contestId = contest.currentId
  if (!contestId) {
    ElMessage.warning('未选择场次')
    return
  }
  if (!entries.total.value) {
    ElMessage.warning('名单为空')
    return
  }

  const target = contest.current
  const head = `名单「${roster.name}」→ 场次「${target?.name ?? ''}」：补入缺少的选手，更新已有选手。`
  const confirmText = prune
    ? head + '不在名单中的选手将被删除，不可恢复；有代码或成绩的选手保留。'
    : head

  try {
    await ElMessageBox.confirm(confirmText, prune ? '应用并清理？' : '应用名单？', {
      type: prune ? 'warning' : 'info',
      confirmButtonText: prune ? '应用并清理' : '应用',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }

  applying.value = true
  try {
    applyReport.value = await rosterApi.applyToContest(contestId, { roster_id: roster.id, prune })
    reportOpen.value = true
    // 场次列表上的人数跟着变了，刷新一下顶栏的计数
    await contest.load()
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    applying.value = false
  }
}

/**
 * 换名单时把关键词清掉。
 *
 * 搜索词是"在这份名单里找谁"，换了一份之后它多半不适用；留着会让新名单看上去
 * 是空的，而教师很难想到是上一份名单的搜索词还在。
 */
watch(selectedRosterId, () => {
  keyword.value = ''
})

// 「按姓名匹配机器」：预览 + 确认在对话框里
const matchOpen = ref(false)

function onRosterCommand(command: string): void {
  if (command === 'prune') void applyToContest(true)
  else if (command === 'delete') askRemoveRoster()
}
</script>

<template>
  <PageShell title="名单库" :error="rosters.error.value" retryable @retry="rosters.reload">
    <template #toolbar>
      <el-button :loading="rosters.loading.value" @click="rosters.reload">
        <el-icon><Refresh /></el-icon>刷新
      </el-button>
      <el-button type="primary" @click="openCreate">
        <el-icon><Plus /></el-icon>新建名单
      </el-button>
    </template>

    <SectionCard title="名单" :meta="`${rosters.rows.value.length} 份`">
      <template #extra>
        <template v-if="selectedRoster">
          <el-button
            type="primary"
            plain
            :loading="applying"
            :disabled="!contest.currentId || !entries.total.value"
            @click="applyToContest(false)"
          >
            应用到「{{ contest.current?.name ?? '未选场次' }}」
          </el-button>
          <el-button :disabled="!entries.total.value" @click="matchOpen = true">
            <el-icon><Connection /></el-icon>按姓名匹配机器
          </el-button>
          <el-button @click="openEdit(selectedRoster)">
            <el-icon><Edit /></el-icon>编辑
          </el-button>
          <el-dropdown trigger="click" @command="(command: string) => onRosterCommand(command)">
            <el-button>
              更多<el-icon class="el-icon--right"><ArrowDown /></el-icon>
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item
                  command="prune"
                  :disabled="applying || !contest.currentId || !entries.total.value"
                >
                  <span class="danger-text">应用并清理</span>
                </el-dropdown-item>
                <el-dropdown-item command="delete" divided>
                  <span class="danger-text">删除名单</span>
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </template>
      </template>

      <div class="picker">
        <el-select
          v-model="selectedRosterId"
          placeholder="选择名单"
          style="width: 280px"
          :loading="rosters.loading.value"
          :disabled="!rosters.rows.value.length"
        >
          <el-option
            v-for="roster in rosters.rows.value"
            :key="roster.id"
            :label="`${roster.name}（${roster.entry_count} 人）`"
            :value="roster.id"
          />
        </el-select>
        <span v-if="selectedRoster" class="muted">
          {{ selectedRoster.entry_count }} 人
          <template v-if="selectedRoster.note"> · {{ selectedRoster.note }}</template>
        </span>
        <span v-else-if="!rosters.loading.value && !rosters.rows.value.length" class="muted">
          无名单
        </span>
      </div>
    </SectionCard>

    <SectionCard
      title="名单条目"
      :meta="selectedRoster ? `${selectedRoster.name} · ${entries.total.value ?? 0} 人` : ''"
      flush
    >
      <template #extra>
        <el-input
          v-model="keyword"
          placeholder="筛选考号 / 姓名 / 座位"
          clearable
          style="width: 200px"
        >
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
        <el-button
          v-if="entries.hasSelection.value"
          type="danger"
          plain
          :loading="batchRemoving"
          @click="batchRemoveEntries"
        >
          删除选中（{{ entries.selectedKeys.value.length }} 条）
        </el-button>
        <el-button type="primary" plain :disabled="!selectedRoster" @click="openImport">
          <el-icon><Upload /></el-icon>粘贴导入
        </el-button>
        <el-dropdown trigger="click" @command="askClearEntries">
          <el-button :disabled="!entries.total.value">
            更多<el-icon class="el-icon--right"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="clear">
                <span class="danger-text">清空条目</span>
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </template>

      <el-alert
        v-if="entries.error.value"
        type="error"
        :closable="false"
        show-icon
        :title="`名单条目加载失败：${entries.error.value}`"
        style="margin: 12px 20px"
      />

      <DataTable
        :rows="visibleEntries"
        :row-key="(row: RosterEntryOut) => row.id"
        :loading="entries.loading.value"
        :total="entries.total.value"
        :page="entries.page.value"
        :page-size="entries.pageSize.value"
        selectable
        :empty-text="selectedRoster ? '名单为空' : '未选择名单'"
        @update:page="entries.setPage"
        @update:pageSize="entries.setPageSize"
        @selection-change="entries.onSelectionChange"
      >
        <template #columns>
          <el-table-column label="考号" width="160">
            <template #default="{ row }">
              <span class="mono">{{ row.player_no }}</span>
            </template>
          </el-table-column>

          <el-table-column label="姓名" width="140">
            <template #default="{ row }">{{ row.name || '—' }}</template>
          </el-table-column>

          <el-table-column label="座位" width="110">
            <template #default="{ row }">{{ row.seat || '—' }}</template>
          </el-table-column>

          <el-table-column label="分组" min-width="120">
            <template #default="{ row }">{{ row.group_name || '—' }}</template>
          </el-table-column>

          <el-table-column label="操作" width="130" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openEntry(row)">编辑</el-button>
              <el-button link type="danger" size="small" @click="askRemoveEntry(row)">删除</el-button>
            </template>
          </el-table-column>
        </template>

        <template #empty>
          <template v-if="selectedRoster">
            <p v-if="keyword.trim()">无匹配「{{ keyword.trim() }}」的条目</p>
            <template v-else>
              <p>名单为空</p>
              <el-button type="primary" @click="openImport">
                <el-icon><Upload /></el-icon>粘贴导入
              </el-button>
            </template>
          </template>
          <template v-else>
            <p>无名单</p>
            <el-button type="primary" @click="openCreate">
              <el-icon><Plus /></el-icon>新建名单
            </el-button>
          </template>
        </template>
      </DataTable>
    </SectionCard>

    <!-- 新建 / 编辑名单 -->
    <FormDialog
      v-model="formOpen"
      :title="formTitle"
      :submitting="saveRoster.pending.value"
      :disabled="!formName.trim()"
      confirm-text="保存"
      @submit="saveRoster.run(undefined)"
    >
      <el-form label-width="72px">
        <el-form-item label="名称">
          <el-input v-model="formName" maxlength="200" placeholder="例如 高一(1)班" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="formNote" maxlength="500" placeholder="例如 2025 届" />
        </el-form-item>
      </el-form>
    </FormDialog>

    <!-- 编辑单条 -->
    <FormDialog
      v-model="entryOpen"
      title="编辑名单条目"
      :submitting="saveEntry.pending.value"
      :disabled="!entryNo.trim()"
      confirm-text="保存"
      @submit="saveEntry.run(undefined)"
    >
      <el-form label-width="72px">
        <el-form-item label="考号">
          <el-input v-model="entryNo" maxlength="64" placeholder="例如 S001" />
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="entryName" maxlength="64" placeholder="例如 张三" />
        </el-form-item>
        <el-form-item label="座位">
          <el-input v-model="entrySeat" maxlength="32" placeholder="例如 A1" />
        </el-form-item>
        <el-form-item label="分组">
          <el-input v-model="entryGroup" maxlength="64" placeholder="例如 A组" />
        </el-form-item>
      </el-form>
    </FormDialog>

    <!-- 粘贴导入 -->
    <el-dialog
      v-model="importVisible"
      title="粘贴导入名单"
      width="720px"
      :close-on-click-modal="false"
    >
      <div class="format">格式 <span class="mono">考号,姓名,座位,分组</span></div>

      <el-input
        v-model="raw"
        type="textarea"
        :rows="10"
        placeholder="S001,张三,A1,A组&#10;S002,李四,A2,A组&#10;S003,王五,B1,B组"
      />

      <div v-if="parsed.length" class="preview">
        <div class="preview-head">
          <span>预览</span>
          <span>
            <el-tag type="success" effect="plain">{{ validRows.length }} 条可导入</el-tag>
            <el-tag v-if="invalidRows.length" type="danger" effect="plain" style="margin-left: 6px">
              {{ invalidRows.length }} 条跳过
            </el-tag>
          </span>
        </div>

        <el-table :data="parsed" max-height="240">
          <el-table-column label="行" prop="line" width="55" align="right" />
          <el-table-column label="考号" prop="player_no" width="110">
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
          <el-table-column label="校验" min-width="140">
            <template #default="{ row }">
              <el-tag v-if="row.error" type="danger" effect="light">{{ row.error }}</el-tag>
              <el-tag v-else type="success" effect="plain">正常</el-tag>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <template #footer>
        <el-button @click="importVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="importEntries.pending.value"
          :disabled="!validRows.length"
          @click="importEntries.run(undefined)"
        >
          导入 {{ validRows.length }} 条
        </el-button>
      </template>
    </el-dialog>

    <!-- 应用回执 -->
    <el-dialog v-model="reportOpen" title="应用结果" width="560px">
      <template v-if="applyReport">
        <p class="report-line">
          名单「{{ applyReport.roster_name || selectedName }}」→ 场次「{{ contest.current?.name }}」
        </p>

        <el-row :gutter="8">
          <el-col :span="6">
            <div class="stat-value">{{ applyReport.created }}</div>
            <div class="stat-label">新增</div>
          </el-col>
          <el-col :span="6">
            <div class="stat-value">{{ applyReport.updated }}</div>
            <div class="stat-label">更新</div>
          </el-col>
          <el-col :span="6">
            <div class="stat-value">{{ applyReport.kept }}</div>
            <div class="stat-label">保留</div>
          </el-col>
          <el-col :span="6">
            <div class="stat-value" :class="{ warn: applyReport.pruned > 0 }">
              {{ applyReport.pruned }}
            </div>
            <div class="stat-label">移除</div>
          </el-col>
        </el-row>

        <el-alert
          v-if="applyReport.protected?.length"
          type="warning"
          :closable="false"
          show-icon
          style="margin-top: 14px"
        >
          <template #title>{{ applyReport.protected.length }} 人有代码或成绩，未移除</template>
          <template #default>
            <div class="mono">{{ applyReport.protected.join('、') }}</div>
          </template>
        </el-alert>
      </template>

      <template #footer>
        <el-button type="primary" @click="reportOpen = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 删除名单：名单名称逐字确认 -->
    <ConfirmByNameDialog
      v-model="deleteOpen"
      :title="`删除名单「${deleteTarget?.name ?? ''}」？`"
      :expected="deleteTarget?.name ?? ''"
      :submitting="removeRoster.pending.value"
      confirm-text="删除"
      :detail="deleteRosterDetail"
      @confirm="removeRoster.run(undefined)"
    />

    <!-- 删除单条：考号逐字确认 -->
    <ConfirmByNameDialog
      v-model="entryDeleteOpen"
      :title="`删除条目「${entryDeleteTarget?.player_no ?? ''}」？`"
      :expected="entryDeleteTarget?.player_no ?? ''"
      :submitting="removeEntry.pending.value"
      confirm-text="删除"
      :detail="
        `将从名单「${selectedName}」删除 ${entryDeleteTarget?.player_no ?? ''}` +
        `${entryDeleteTarget?.name ? ` ${entryDeleteTarget.name}` : ''}，不可恢复；各场次选手不受影响。`
      "
      @confirm="removeEntry.run(undefined)"
    />

    <!-- 清空条目：名单名称逐字确认 -->
    <ConfirmByNameDialog
      v-model="clearOpen"
      title="清空名单条目？"
      :expected="selectedName"
      :submitting="clearEntries.pending.value"
      confirm-text="清空"
      :detail="
        `将删除名单「${selectedName}」的全部 ${entries.total.value} 条记录，不可恢复；` +
        '名单本身与各场次选手保留。'
      "
      @confirm="clearEntries.run(undefined)"
    />
    <RosterMatchDialog
      v-model="matchOpen"
      :roster-id="selectedRosterId"
      :roster-name="selectedName"
    />
  </PageShell>
</template>

<style scoped>
.picker {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
}

.report-line {
  margin: 0 0 12px;
  font-weight: 600;
}

.stat-value {
  font-size: 20px;
  font-weight: 600;
  line-height: 1.2;
}

.stat-value.warn {
  color: var(--el-color-warning);
}

.stat-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 2px;
  line-height: 1.3;
}

.format {
  margin-bottom: 8px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
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

.muted {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.danger-text {
  color: var(--el-color-danger);
}
</style>

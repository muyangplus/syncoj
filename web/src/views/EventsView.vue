<script setup lang="ts">
import { computed, ref } from 'vue'

import { eventApi } from '@/api'
import { GLOBAL_CONFIRM } from '@/api/endpoints'
import type { EventOut } from '@/api/types'
import ConfirmByNameDialog from '@/components/ConfirmByNameDialog.vue'
import DataTable from '@/components/DataTable.vue'
import HelpTip from '@/components/HelpTip.vue'
import PageShell from '@/components/PageShell.vue'
import { useList } from '@/composables/useList'
import { useMutation } from '@/composables/useMutation'
import { useContestStore } from '@/stores/contest'
import { eventCategoryLabel, eventLevelLabel, formatTime } from '@/utils/format'

const contest = useContestStore()

/** 事件范围。默认「全部」—— 全局视图恰恰是原先缺的那一个。 */
type EventScope = 'all' | 'contest'
const scope = ref<EventScope>('all')

const level = ref<string>('')
const category = ref<string>('')

/**
 * 页面内的关键词过滤。
 *
 * 服务端的事件接口只认 `limit`/`offset`/`level`/`category`，**没有** `q`。
 * 所以输入框旁边的说明写「本页内搜索」，而不是含混的「搜索」：过滤只作用在
 * 已经取回来的这一页上，让人以为搜不到就是没有，比不提供搜索更糟。
 */
const keyword = ref('')

const LEVELS = ['info', 'warning', 'error']

/**
 * 可以用来筛选/清空的分类。
 *
 * 这里列的是**服务端真的会写进 `EventLog.category` 的那些值**（对着
 * `api/admin.py`、`services/`、`tasks/` 里的 `category=` 抄的），不是凭印象编的 ——
 * 下拉里给一个永远不会出现的分类，看起来像功能，实际是误导。
 */
const CATEGORIES = [
  // 注册与配对（发生在配对之前，只有「全部」范围看得到）
  'enroll',
  'enroll_conflict',
  'enroll_ambiguous',
  'bind',
  'bootstrap_key',
  'machines_clear',
  // 回收与代码
  'file_deleted',
  'file_restored',
  'bulk_rewrite',
  'scan_rejected',
  'scan_incomplete',
  'files_clear',
  // 机器与场次
  'offline',
  'agent_rebind',
  'agent_unbind',
  'agent_revoke',
  'contest_delete',
  'roster_apply',
  'player_delete',
  'players_clear',
  // 下发
  'deploy_created',
  'deploy_cancelled',
  'deploy_delete',
  // 题目与成绩
  'problem_import',
  'problem_clear',
  'judge_manual',
  'scores_clear',
  // 发布
  'release_uploaded',
  'release_rollout',
  'release_yank',
  // 日志自身：清空日志会留下这一条，它可以作为"上次是谁清的"的入口
  'events_clear',
]

const list = useList<EventOut>({
  rowKey: (row) => row.id,
  loader: ({ limit, offset, signal }) => {
    const params = {
      limit,
      offset,
      level: level.value || undefined,
      category: category.value || undefined,
    }
    // 「当前场次」却没选场次时返回 null：列表空着但**不报错** —— 没选场次不是错误
    if (scope.value === 'contest') {
      const contestId = contest.currentId
      if (!contestId) return Promise.resolve(null)
      return eventApi.list(contestId, params, signal)
    }
    return eventApi.listAll(params, signal)
  },
  // 8 秒一轮：告警类是"机器刚出问题就要看到"的东西，慢半分钟就没意义了
  interval: 8000,
  // 切换范围/场次/级别/分类都回第一页并重拉，不再自己写 watch + reload
  watches: [() => scope.value, () => contest.currentId, () => level.value, () => category.value],
})

/** 本页内的关键词过滤。跨页搜索要靠级别与分类这两个服务端参数。 */
const rows = computed(() => {
  const needle = keyword.value.trim().toLowerCase()
  if (!needle) return list.rows.value
  return list.rows.value.filter((row) =>
    [row.message, row.player_no ?? '', row.category, eventCategoryLabel(row.category)]
      .join('\n')
      .toLowerCase()
      .includes(needle),
  )
})

/** 范围说明，清空对话框与提示里共用 —— 一处定义，不给出两套说法。 */
const scopeLabel = computed(() => {
  if (scope.value === 'contest') {
    return contest.current ? `当前场次 ${contest.current.slug}` : '当前场次（还没选场次）'
  }
  return '全部场次（含不属于任何场次的全局告警）'
})

function levelType(level: string): 'info' | 'warning' | 'danger' {
  if (level === 'error') return 'danger'
  if (level === 'warning') return 'warning'
  return 'info'
}

/** 一行里能读出的摘要。展开后才看完整 meta —— 它一般有几个字段。 */
function metaSummary(meta: EventOut['meta']): string {
  if (!meta) return ''
  const entries = Object.entries(meta)
  if (!entries.length) return ''
  return entries
    .slice(0, 2)
    .map(([key, value]) => `${key}=${shortValue(value)}`)
    .join('  ')
}

function shortValue(value: unknown): string {
  if (value === null || value === undefined) return String(value)
  if (typeof value === 'object') return Array.isArray(value) ? `[${value.length} 项]` : '{…}'
  const text = String(value)
  return text.length > 24 ? `${text.slice(0, 24)}…` : text
}

/**
 * 把 meta 展开成可读的 JSON。
 *
 * 它是不定形的对象（不同分类的字段完全不一样），所以不硬编码字段名：
 * 服务端新加一个字段时，界面不用跟着改也能看见。字段名不翻译 —— 它们大多
 * 对应库里的列名，翻译之后反而对不上。
 */
function metaText(meta: EventOut['meta']): string {
  if (!meta) return ''
  try {
    return JSON.stringify(meta, null, 2)
  } catch {
    // 理论上到不了这里（meta 已经解析成对象了），但 meta 里要是有循环引用或
    // 大整数，JSON.stringify 会抛 —— 那时退回能显示多少算多少，而不是白屏
    return String(meta)
  }
}

// --------------------------------------------------------------------------- //
// 清空日志
//
// 这是**硬删除**（服务端 `session.delete`，没有墓碑）。二次确认的形式是"把名字
// 打一遍"，但打的是**范围**的名字而不是某个对象的名字：指定场次就输它的标识，
// 全局清空输 `all`（`GLOBAL_CONFIRM`）。这就是约定里"范围删除用范围的名字"那一条。
//
// 默认值刻意不激进：默认只看「早于 7 天」，因为服务端那句话是对的 ——
// 一把清空很容易把"三天前那台机器为什么掉线"的唯一线索抹掉。
// --------------------------------------------------------------------------- //

const clearOpen = ref(false)
/**
 * 清空范围与列表上的「范围」开关**分开**。
 *
 * 它们看起来是同一件事，但不该是同一个变量：列表那个开关是"我想看什么"，
 * 而这个选择是"我要删什么"。改成列表筛选就顺手改了删除范围，是最危险的耦合。
 * 打开对话框时从当前视图带个初值过去，之后各管各的。
 */
const clearScope = ref<EventScope>('all')
/** 天数上限：0 表示不限，其余是"早于 N 天"。 */
const clearDays = ref(7)
/** 清空的级别范围：只清信息级，还是连警告/错误一起清。 */
const clearLevel = ref<'info' | ''>('info')

const clearScopeLabel = computed(() => {
  if (clearScope.value === 'contest') {
    return contest.current ? `当前场次 ${contest.current.slug}` : '当前场次（还没选场次）'
  }
  return '全部场次（含不属于任何场次的全局告警）'
})

const clearScopeText = computed(() => {
  const levelText =
    clearLevel.value === 'info' ? '只清「信息」级' : '清「警告」与「错误」级（含信息级）'
  const daysText = clearDays.value > 0 ? `早于 ${clearDays.value} 天` : '不限天数'
  return `${clearScopeLabel.value} · ${levelText} · ${daysText}`
})

/** 「全部场次 + 不限天数」是可以一举抹掉全部线索的那一档，单独标出来。 */
const clearIsTotal = computed(() => clearScope.value === 'all' && clearDays.value === 0)

function openClear(): void {
  clearScope.value = scope.value
  clearOpen.value = true
}

/**
 * 清空要输入的确认内容。
 *
 * 它跟着**范围**走而不是跟着某个对象走：清一个场次输场次标识，清全部输
 * `all`（服务端的 `GLOBAL_CONFIRM`）。所以对话框里要问的那个名字是"这片日志
 * 属于谁"，而这两者拼出来的字面量完全没有相似之处，教师不可能靠肌肉记忆蒙对。
 */
const clearConfirmName = computed(() =>
  clearScope.value === 'contest' ? (contest.current?.slug ?? '') : GLOBAL_CONFIRM,
)

const clearConfirmPlaceholder = computed(() =>
  clearScope.value === 'contest'
    ? `输入场次标识 ${contest.current?.slug ?? ''}`
    : `全部场次要输入 ${GLOBAL_CONFIRM}`,
)

const clearEvents = useMutation(
  () =>
    eventApi.clear({
      confirm: clearConfirmName.value,
      contestId: clearScope.value === 'contest' ? (contest.currentId ?? undefined) : undefined,
      level: clearLevel.value || undefined,
      // 0 不发给服务端：`older_than_days` 缺省才是"不管多久以前"
      olderThanDays: clearDays.value > 0 ? clearDays.value : undefined,
    }),
  {
    onDone: async () => {
      clearOpen.value = false
      await list.reload()
    },
  },
)

/** 当前筛选下有多少条 —— 确认文案里写真实数字，而不是只说"全部"。 */
const clearCountHint = computed(() => {
  if (list.error.value) return '当前筛选范围的条数读不到（列表加载出错）'
  return `当前筛选范围共 ${list.total.value} 条；实际清掉哪些，由下面选的范围决定`
})
</script>

<template>
  <PageShell
    title="审计日志"
    :error="list.error.value"
    error-action="日志取不到时，别用筛选条件去推断发生过什么。"
    retryable
    @retry="list.reload"
  >
    <template #hint>
      <HelpTip>
        只增不改；默认「全部」才看得到配对前的全局告警。
      </HelpTip>
    </template>

    <template #toolbar>
      <el-radio-group v-model="scope" size="small">
        <el-radio-button value="all">全部</el-radio-button>
        <el-radio-button value="contest">当前场次</el-radio-button>
      </el-radio-group>
      <el-select v-model="level" size="small" placeholder="全部级别" clearable style="width: 120px">
        <el-option
          v-for="item in LEVELS"
          :key="item"
          :label="eventLevelLabel(item)"
          :value="item"
        />
      </el-select>
      <el-select
        v-model="category"
        size="small"
        placeholder="全部分类"
        clearable
        style="width: 150px"
      >
        <el-option
          v-for="item in CATEGORIES"
          :key="item"
          :label="eventCategoryLabel(item)"
          :value="item"
        />
      </el-select>
      <el-input
        v-model="keyword"
        size="small"
        placeholder="本页内搜索内容或分类"
        clearable
        style="width: 200px"
      />
      <el-button size="small" :loading="list.loading.value" @click="list.reload">刷新</el-button>
      <el-button size="small" type="danger" plain @click="openClear">清空日志</el-button>
    </template>

    <p class="page-hint" style="margin-bottom: 8px">范围：{{ scopeLabel }}。</p>

    <DataTable
      :rows="rows"
      :row-key="(row: EventOut) => row.id"
      :loading="list.loading.value"
      :total="list.total.value"
      :page="list.page.value"
      :page-size="list.pageSize.value"
      empty-text="这个范围里还没有审计事件"
      @update:page="list.setPage"
      @update:pageSize="list.setPageSize"
    >
      <template #columns>
        <!--
          展开行而不是弹窗：先扫一眼"这条事件带了什么字段"是很常见的动作，
          每次都要点一下、看一下、关掉，比直接铺在行下难用得多。
        -->
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="meta-block">
              <div v-if="!row.meta" class="muted">这条事件没有附带细节字段。</div>
              <pre v-else class="meta-pre">{{ metaText(row.meta) }}</pre>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="时间" width="170">
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

        <el-table-column label="分类" width="130">
          <template #default="{ row }">
            <!-- 未知分类照原样显示：服务端新加一类时，它不该在界面上消失 -->
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
            <el-tooltip v-if="row.meta" :content="'展开可看完整字段'">
              <span class="muted mono">{{ metaSummary(row.meta) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>
      </template>

      <template #empty>
        <p v-if="scope === 'contest' && !contest.currentId">还没选场次。</p>
        <template v-else>
          <p class="page-hint">
            这个范围里还没有审计事件；刚注册的机器看不到时请把范围切回「全部」。
          </p>
        </template>
      </template>
    </DataTable>

    <!--
      清空日志是硬删除，而且**有**二次确认 —— 但服务端要的不是某个对象的名字，
      是**范围**的名字：指定场次就输该场次的标识，全局清空输 `all`
      （`GLOBAL_CONFIRM`）。所以对话框里除了"打一遍名字"，还要把范围本身
      （哪些场次、哪个级别、早于几天）摆在同一屏上，三件事一个都不能含糊。
    -->
    <ConfirmByNameDialog
      v-model="clearOpen"
      title="清空审计日志"
      :expected="clearConfirmName"
      :placeholder="clearConfirmPlaceholder"
      :submitting="clearEvents.pending.value"
      :detail="`即将清空：${clearScopeText}。${clearCountHint}。`"
      confirm-text="清空"
      @confirm="clearEvents.run(undefined)"
    >
      <el-form label-width="90px">
        <el-form-item label="场次范围">
          <el-radio-group v-model="clearScope" size="small">
            <el-radio value="all">全部场次</el-radio>
            <el-radio value="contest" :disabled="!contest.currentId">当前场次</el-radio>
          </el-radio-group>
          <div class="muted">
            全部场次 = 所有场次的日志 + 不属于任何场次的全局告警（统一密钥注册、克隆指纹）。
          </div>
        </el-form-item>

        <el-form-item label="级别范围">
          <el-radio-group v-model="clearLevel" size="small">
            <el-radio value="info">只清「信息」级</el-radio>
            <el-radio value="">连警告与错误一起清</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item label="时间范围">
          <el-radio-group v-model="clearDays" size="small">
            <el-radio :value="1">早于 1 天</el-radio>
            <el-radio :value="7">早于 7 天</el-radio>
            <el-radio :value="30">早于 30 天</el-radio>
            <el-radio :value="0">不限天数</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>

      <!-- 两档危险范围各自给一块显眼的说明，而不是只靠上面那行灰字 -->
      <el-alert
        v-if="clearDays === 0"
        type="error"
        :closable="false"
        show-icon
        style="margin-top: 4px"
        title="不限天数会连最老的日志一起删掉"
      />

      <el-alert
        v-if="clearIsTotal"
        type="error"
        :closable="false"
        show-icon
        style="margin-top: 8px"
        title="这是最彻底的一档：全部场次 + 不限天数"
      >
        <template #default>
          执行后只会留下一条「某人在某时清理了审计日志」的记录。
        </template>
      </el-alert>

      <div v-else class="clear-scope mono">{{ clearScopeText }}</div>
    </ConfirmByNameDialog>
  </PageShell>
</template>

<style scoped>
.meta-block {
  padding: 8px 12px;
  background: #f5f7fa;
  border-radius: 4px;
}

.meta-pre {
  margin: 0;
  max-height: 360px;
  overflow: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}

.muted {
  color: #909399;
  font-size: 12px;
}

.clear-scope {
  margin-top: 12px;
  padding: 8px 12px;
  background: #fef0f0;
  border: 1px solid #fde2e2;
  border-radius: 4px;
  color: #f56c6c;
  font-size: 12px;
}
</style>

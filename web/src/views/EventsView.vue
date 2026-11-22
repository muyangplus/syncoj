<script setup lang="ts">
import { computed, ref } from 'vue'

import { eventApi } from '@/api'
import { GLOBAL_CONFIRM } from '@/api/endpoints'
import type { EventOut } from '@/api/types'
import ConfirmByNameDialog from '@/components/ConfirmByNameDialog.vue'
import DataTable from '@/components/DataTable.vue'
import PageShell from '@/components/PageShell.vue'
import SectionCard from '@/components/SectionCard.vue'
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
    return contest.current ? `场次 ${contest.current.slug}` : '当前场次（未选择）'
  }
  return '全部场次（含全局事件）'
})

const clearScopeText = computed(() => {
  const levelText = clearLevel.value === 'info' ? '仅信息级' : '全部级别'
  const daysText = clearDays.value > 0 ? `早于 ${clearDays.value} 天` : '不限时间'
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
    ? `输入 ${contest.current?.slug ?? ''}`
    : `输入 ${GLOBAL_CONFIRM}`,
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
  if (list.error.value) return '列表当前筛选：条数未知'
  return `列表当前筛选：${list.total.value} 条`
})
</script>

<template>
  <PageShell title="审计日志" :error="list.error.value" retryable @retry="list.reload">
    <template #toolbar>
      <el-button :loading="list.loading.value" @click="list.reload">
        <el-icon><Refresh /></el-icon>刷新
      </el-button>
      <el-dropdown trigger="click">
        <el-button>
          更多<el-icon class="el-icon--right"><ArrowDown /></el-icon>
        </el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item @click="openClear">
              <span class="danger-item">清空日志</span>
            </el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </template>

    <SectionCard title="事件" :meta="`共 ${list.total.value} 条`" flush>
      <template #extra>
        <el-radio-group v-model="scope">
          <el-radio-button value="all">全部</el-radio-button>
          <el-radio-button value="contest">当前场次</el-radio-button>
        </el-radio-group>
        <el-select v-model="level" placeholder="全部级别" clearable style="width: 120px">
          <el-option
            v-for="item in LEVELS"
            :key="item"
            :label="eventLevelLabel(item)"
            :value="item"
          />
        </el-select>
        <el-select v-model="category" placeholder="全部分类" filterable clearable style="width: 150px">
          <el-option
            v-for="item in CATEGORIES"
            :key="item"
            :label="eventCategoryLabel(item)"
            :value="item"
          />
        </el-select>
        <el-input v-model="keyword" placeholder="本页搜索内容 / 分类" clearable style="width: 190px">
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
      </template>

      <DataTable
        :rows="rows"
        :row-key="(row: EventOut) => row.id"
        :loading="list.loading.value"
        :total="list.total.value"
        :page="list.page.value"
        :page-size="list.pageSize.value"
        empty-text="无审计事件"
        @update:page="list.setPage"
        @update:pageSize="list.setPageSize"
      >
        <template #columns>
          <el-table-column type="expand">
            <template #default="{ row }">
              <div class="meta-block">
                <div v-if="!row.meta" class="muted">无附加字段</div>
                <pre v-else class="meta-pre">{{ metaText(row.meta) }}</pre>
              </div>
            </template>
          </el-table-column>

          <el-table-column label="时间" width="170">
            <template #default="{ row }">
              <span class="cell-sub">{{ formatTime(row.ts) }}</span>
            </template>
          </el-table-column>

          <el-table-column label="级别" width="90">
            <template #default="{ row }">
              <el-tag :type="levelType(row.level)" effect="light">
                {{ eventLevelLabel(row.level) }}
              </el-tag>
            </template>
          </el-table-column>

          <el-table-column label="分类" width="140">
            <template #default="{ row }">
              <el-tooltip :content="row.category">
                <span>{{ eventCategoryLabel(row.category) }}</span>
              </el-tooltip>
            </template>
          </el-table-column>

          <el-table-column label="考号" width="100">
            <template #default="{ row }">
              <span class="mono">{{ row.player_no || '—' }}</span>
            </template>
          </el-table-column>

          <el-table-column label="内容" min-width="320">
            <template #default="{ row }">
              <div>{{ row.message }}</div>
              <span v-if="row.meta" class="cell-sub mono">{{ metaSummary(row.meta) }}</span>
            </template>
          </el-table-column>
        </template>
      </DataTable>
    </SectionCard>

    <!-- 清空日志：服务端硬删；confirm 为范围名（场次标识或 GLOBAL_CONFIRM） -->
    <ConfirmByNameDialog
      v-model="clearOpen"
      title="清空审计日志？"
      :expected="clearConfirmName"
      :placeholder="clearConfirmPlaceholder"
      :submitting="clearEvents.pending.value"
      :impact="[`清空：${clearScopeText}`, clearCountHint]"
      detail="将删除范围内的审计日志，不可恢复。"
      confirm-text="清空"
      @confirm="clearEvents.run(undefined)"
    >
      <el-form label-width="80px">
        <el-form-item label="场次">
          <el-radio-group v-model="clearScope">
            <el-radio value="all">全部场次</el-radio>
            <el-radio value="contest" :disabled="!contest.currentId">当前场次</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item label="级别">
          <el-radio-group v-model="clearLevel">
            <el-radio value="info">仅信息</el-radio>
            <el-radio value="">全部级别</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item label="时间">
          <el-radio-group v-model="clearDays">
            <el-radio :value="1">早于 1 天</el-radio>
            <el-radio :value="7">早于 7 天</el-radio>
            <el-radio :value="30">早于 30 天</el-radio>
            <el-radio :value="0">不限</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>

      <el-alert
        v-if="clearDays === 0"
        type="error"
        :closable="false"
        show-icon
        :title="
          clearIsTotal
            ? '将删除全部审计日志，仅留下本次清理记录。'
            : '将删除该范围内所有时间的日志。'
        "
      />
    </ConfirmByNameDialog>
  </PageShell>
</template>

<style scoped>
.meta-block {
  padding: 8px 12px;
  background: var(--sj-surface-2);
  border: 1px solid var(--sj-border);
  border-radius: var(--sj-radius-sm);
}

.meta-pre {
  margin: 0;
  max-height: 360px;
  overflow: auto;
  font-family: var(--sj-mono);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}

.danger-item {
  color: var(--sj-danger);
}
</style>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import { problemApi } from '@/api'
import type { ProblemMatchOut, ProblemOut, ProblemUpsert } from '@/api/types'
import ConfirmByNameDialog from '@/components/ConfirmByNameDialog.vue'
import DataTable from '@/components/DataTable.vue'
import FormDialog from '@/components/FormDialog.vue'
import ProblemJudgeDialog from '@/components/ProblemJudgeDialog.vue'
import SectionCard from '@/components/SectionCard.vue'
import { useList } from '@/composables/useList'
import { useMutation } from '@/composables/useMutation'
import { useContestStore } from '@/stores/contest'
import { parsePasteLines } from '@/utils/paste'

const props = defineProps<{
  modelValue: boolean
  contestId: number | null
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  changed: []
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const raw = ref('')

// --------------------------------------------------------------------------- //
// 题目清单
//
// 用 useList 而不是自己管 loading/error：清单接口现在返回的分页信封由数据层
// 统一吃掉，页面只管"这一栏显示哪几列"。`watches` 收场次 id —— 换场次要回第一页
// 重拉，而且 loader 在没有场次时返回 null（列表空着但**不报错**）。
// --------------------------------------------------------------------------- //

const problems = useList<ProblemOut>({
  rowKey: (row) => row.id,
  loader: ({ limit, offset, signal }) => {
    const contestId = props.contestId
    if (!contestId) return Promise.resolve(null)
    return problemApi.list(contestId, { limit, offset }, signal)
  },
  watches: [() => props.contestId],
})

/** 打开或换场次时重新拉一次：弹窗里的数据可能已经过期很久了。 */
watch(visible, (open) => {
  if (!open) return
  raw.value = ''
  closeEdit()
  probePath.value = ''
  probeResult.value = null
  void problems.reload()
})

// --------------------------------------------------------------------------- //
// 批量登记（粘贴）
// --------------------------------------------------------------------------- //

interface ParsedRow {
  line: number
  ident: string
  title: string
  /** 第三列起的 glob 模式；空表示用服务端默认值。 */
  patterns: string[]
  error?: string
}

/**
 * 按行解析粘贴的题目清单。
 *
 * 每行 ``标识``、``标识,标题`` 或 ``标识,标题,模式``（逗号、制表符、中文逗号都认，
 * 分隔符顺序见 ``utils/paste.ts``）。
 * 第三列里的多条模式用 ``;`` 或 ``|`` 分隔 —— 不用空格，因为空格在"整行按空白
 * 分列"的形式下本身就是列分隔符，两种含义混在一起没人猜得准。
 *
 * 标识会同时成为目录名、代码文件名与成绩矩阵列名，所以这里能做的格式检查
 * 尽量做掉，真正的合法性由服务端判定。
 */
function splitPatterns(cell: string): string[] {
  return cell
    .split(/[;|]/)
    .map((item) => item.trim())
    .filter(Boolean)
}

const parsed = computed<ParsedRow[]>(() => {
  const seen = new Set<string>()
  const rows: ParsedRow[] = []

  for (const { line, columns } of parsePasteLines(raw.value)) {
    // 整行按空白分列时，第三列之后的每一段都当成一条模式 —— 教师直接
    // 从文档里粘 `p1 签到题 p1/**` 是很自然的写法
    const [ident = '', title = ''] = columns
    const tail = columns.slice(2)
    const patterns = tail.length > 1 ? tail.filter(Boolean) : splitPatterns(tail[0] ?? '')

    const row: ParsedRow = { line, ident, title, patterns }

    if (!ident) {
      row.error = '缺少题目标识'
    } else if (ident.length > 64) {
      row.error = '标识超过 64 字符'
    } else if (ident.includes('/') || ident.includes('\\')) {
      // 标识必须是单个目录名 —— 带斜杠会在客户端拼出意外的目录层级
      row.error = '标识不能包含斜杠'
    } else if (patterns.length > 16) {
      row.error = '单题最多 16 个模式'
    } else if (seen.has(ident)) {
      row.error = '本次粘贴中重复'
    } else {
      seen.add(ident)
    }
    rows.push(row)
  }

  return rows
})

const validRows = computed(() => parsed.value.filter((row) => !row.error))
const invalidRows = computed(() => parsed.value.filter((row) => row.error))

const importProblems = useMutation(
  async () => {
    const contestId = props.contestId
    if (!contestId) throw new Error('未选择场次')
    const payload: ProblemUpsert[] = validRows.value.map((row) => ({
      ident: row.ident,
      title: row.title || null,
      // 0 表示"让服务端按提交顺序自动编号"。
      // 注意这个字段不能省：openapi-typescript 会把"有非 None 默认值"的字段
      // 渲染成必填（服务端总会给值），省略它会直接编译不过。
      order_index: 0,
      file_patterns: row.patterns,
    }))
    const result = await problemApi.import(contestId, payload)
    // 服务端是**逐条容错**的：某一条不合法只会出现在 errors 里，其余照常入库。
    // 所以这里走 success 通道，但把那几条被拒的原文单独带出来 —— 只说
    // "已登记 N 道"会让教师以为二十道题都进去了
    return {
      detail: `新增 ${result.created} 道，更新 ${result.updated} 道`,
      errors: result.errors ?? [],
    }
  },
  {
    onDone: async (result) => {
      raw.value = ''
      await problems.reload()
      emit('changed')
      for (const message of result.errors.slice(0, 5)) ElMessage.error(message)
    },
  },
)

// --------------------------------------------------------------------------- //
// 编辑单条
//
// 编辑界面的字段固定在当前正在编辑的那一行上。以前把三个字段散在表格列里、
// 靠 `editingId` 判断哪一格变成输入框 —— 于是"保存"按钮要在行末，而一眼看不出
// 到底改了哪几格。收进一个弹窗之后，"这次改的是什么"是完整的。
// --------------------------------------------------------------------------- //

const editOpen = ref(false)
const editTarget = ref<ProblemOut | null>(null)
const editIdent = ref('')
const editTitle = ref('')
const editOrder = ref(0)
/** 一行一个模式；空 = 回到默认模式。 */
const editPatterns = ref('')

function openEdit(problem: ProblemOut): void {
  editTarget.value = problem
  editIdent.value = problem.ident
  editTitle.value = problem.title ?? ''
  editOrder.value = problem.order_index ?? 0
  // 把**当前生效**的模式填进去（含服务端补的默认值），而不是原始存储值 ——
  // 否则教师看不到"现在到底按什么在认领文件"
  editPatterns.value = (problem.file_patterns ?? []).join('\n')
  editOpen.value = true
}

function closeEdit(): void {
  editOpen.value = false
  editTarget.value = null
}

const saveProblem = useMutation(
  async () => {
    const problem = editTarget.value
    if (!problem) throw new Error('没有选中题目')
    const ident = editIdent.value.trim()
    if (!ident) throw new Error('题目标识不能为空')
    const patterns = editPatterns.value
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean)
    await problemApi.update(problem.id, {
      ident,
      title: editTitle.value.trim() || null,
      order_index: editOrder.value,
      file_patterns: patterns,
    })
    return { detail: `已保存题目 ${ident}` }
  },
  {
    onDone: async () => {
      closeEdit()
      await problems.reload()
      emit('changed')
    },
  },
)

// --------------------------------------------------------------------------- //
// 删除单条
//
// 题目是**结构性数据**，硬删除必须打一遍题目标识（`ConfirmByNameDialog`）：
// 「是否确定」在连续调整清单时会被点成肌肉记忆，而打一遍 ident 不会。
// 弹窗里那句"成绩会保留"同样要紧 —— 教师最怕的是删登记顺带删成绩。
// --------------------------------------------------------------------------- //

const deleteOpen = ref(false)
const deleteTarget = ref<ProblemOut | null>(null)

const removeProblem = useMutation(
  () => {
    const problem = deleteTarget.value
    if (!problem) throw new Error('没有选中题目')
    return problemApi.remove(problem.id, problem.ident)
  },
  {
    onDone: async () => {
      deleteOpen.value = false
      deleteTarget.value = null
      await problems.reload()
      emit('changed')
    },
  },
)

function askRemove(problem: ProblemOut): void {
  deleteTarget.value = problem
  deleteOpen.value = true
}

// --------------------------------------------------------------------------- //
// 路径试算
// --------------------------------------------------------------------------- //

/**
 * 「这条路径会算成哪道题」。
 *
 * 刻意走服务端接口而不是在浏览器里自己匹配一遍：glob 只有一份实现，
 * 界面显示的结果和回收时用的结果才**必然**一致。教师排错时最怕的就是
 * 界面说归 p1、实际归了 p2。
 */
const probePath = ref('')
const probeResult = ref<ProblemMatchOut | null>(null)
const probing = ref(false)

const probe = useMutation(
  async () => {
    const contestId = props.contestId
    if (!contestId) throw new Error('未选择场次')
    const path = probePath.value.trim()
    if (!path) throw new Error('路径不能为空')
    return problemApi.match(contestId, path)
  },
  {
    // 试算结果要贴在界面上（命中哪几个模式、展开成什么），不是一个 toast
    success: () => '',
    onDone: (result) => {
      probeResult.value = result
    },
    onError: () => {
      probeResult.value = null
    },
  },
)

async function runProbe(): Promise<void> {
  // 本地也拦一道：空路径打服务端没有意义，而且会覆盖掉上一次的好结果
  if (!props.contestId || !probePath.value.trim()) return
  probing.value = true
  try {
    await probe.run(undefined)
  } finally {
    probing.value = false
  }
}

const problemCount = computed(() => problems.total.value)

// 清空题目：范围删除，场次标识逐字确认
const contestStore = useContestStore()
const contestSlug = computed(
  () => contestStore.contests.find((item) => item.id === props.contestId)?.slug ?? '',
)
const clearOpen = ref(false)

const clearProblems = useMutation(
  () => {
    const contestId = props.contestId
    if (!contestId) throw new Error('未选择场次')
    return problemApi.clear(contestId, contestSlug.value)
  },
  {
    onDone: async () => {
      clearOpen.value = false
      await problems.reload()
      emit('changed')
    },
  },
)

// 评测设置（§12）
const judgeOpen = ref(false)
const judgeTarget = ref<ProblemOut | null>(null)

function openJudge(row: ProblemOut): void {
  judgeTarget.value = row
  judgeOpen.value = true
}
</script>

<template>
  <el-dialog v-model="visible" title="题目清单" width="920px" :close-on-click-modal="false">
    <SectionCard title="题目" :meta="`${problemCount ?? 0} 道`" flush>
      <template #extra>
        <el-button :loading="problems.loading.value" @click="problems.reload">
          <el-icon><Refresh /></el-icon>刷新
        </el-button>
        <el-dropdown trigger="click" @command="clearOpen = true">
          <el-button :disabled="!problemCount || !contestSlug">
            更多<el-icon class="el-icon--right"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="clear">
                <span class="danger-text">清空题目</span>
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </template>
      <DataTable
        :rows="problems.rows.value"
        :row-key="(row: ProblemOut) => row.id"
        :loading="problems.loading.value"
        :total="problems.total.value"
        :page="problems.page.value"
        :page-size="problems.pageSize.value"
        empty-text="无题目"
        @update:page="problems.setPage"
        @update:pageSize="problems.setPageSize"
      >
        <template #columns>
          <el-table-column label="顺序" width="80" align="right">
            <template #default="{ row }">
              <span class="mono">{{ row.order_index }}</span>
            </template>
          </el-table-column>

          <el-table-column label="标识" width="130">
            <template #default="{ row }">
              <span class="mono">{{ row.ident }}</span>
            </template>
          </el-table-column>

          <el-table-column label="标题" width="160">
            <template #default="{ row }">{{ row.title || '—' }}</template>
          </el-table-column>

          <el-table-column label="代码路径" min-width="220">
            <template #default="{ row }">
              <el-tag
                v-for="pattern in row.file_patterns"
                :key="pattern"
                effect="plain"
                class="pattern-tag"
              >
                {{ pattern }}
              </el-tag>
              <span v-if="!row.file_patterns?.length" class="muted">默认 {ident}/{ident}.cpp</span>
            </template>
          </el-table-column>

          <el-table-column label="评测" width="190">
            <template #default="{ row }">
              <template v-if="row.judge_mode === 'builtin'">
                自带 · {{ row.time_limit_ms ?? 1000 }} ms / {{ row.memory_limit_mb ?? 256 }} MB
                <span v-if="row.testdata" class="muted"> · {{ row.testdata.cases }} 个测试点</span>
                <el-tag v-else type="danger" effect="plain">缺数据包</el-tag>
              </template>
              <span v-else class="muted">外部评测器</span>
            </template>
          </el-table-column>

          <el-table-column label="操作" width="180" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
              <el-button link type="primary" size="small" @click="openJudge(row)">评测设置</el-button>
              <el-button link type="danger" size="small" @click="askRemove(row)">删除</el-button>
            </template>
          </el-table-column>
        </template>

        <template #empty>
          <p>无题目</p>
        </template>
      </DataTable>
    </SectionCard>

    <SectionCard title="路径试算">
      <div class="probe-row">
        <el-input
          v-model="probePath"
          placeholder="例如 p1/p1.cpp"
          style="max-width: 380px"
          @keyup.enter="runProbe"
        />
        <el-button :loading="probing" @click="runProbe">试算</el-button>
        <template v-if="probeResult">
          <el-tag v-if="probeResult.problem" type="success" effect="light">
            归属「{{ probeResult.problem }}」
          </el-tag>
          <el-tag v-else type="warning" effect="light">无匹配题目</el-tag>
          <!-- 模板与展开结果都显示 -->
          <span v-if="probeResult.expanded?.length" class="muted mono">
            命中：{{ (probeResult.patterns ?? []).join('  |  ') }}
            <template v-if="probeResult.patterns?.[0] !== probeResult.expanded[0]">
              → {{ probeResult.expanded.join('  |  ') }}
            </template>
          </span>
        </template>
      </div>
    </SectionCard>

    <SectionCard title="批量登记" meta="标识,标题,代码路径">
      <el-input
        v-model="raw"
        type="textarea"
        :rows="6"
        placeholder="p1,签到题&#10;p2,图论,{ident}/**;src/**&#10;p3"
      />

      <div v-if="parsed.length" class="preview">
        <el-tag type="success" effect="plain">{{ validRows.length }} 道可登记</el-tag>
        <el-tag v-if="invalidRows.length" type="danger" effect="plain">
          {{ invalidRows.length }} 道跳过
        </el-tag>
        <span v-for="row in invalidRows.slice(0, 5)" :key="row.line" class="bad-line">
          第 {{ row.line }} 行：{{ row.error }}
        </span>
      </div>

      <el-button
        type="primary"
        :loading="importProblems.pending.value"
        :disabled="!validRows.length"
        style="margin-top: 10px"
        @click="importProblems.run(undefined)"
      >
        登记 {{ validRows.length }} 道
      </el-button>
    </SectionCard>

    <!-- 编辑单条 -->
    <FormDialog
      v-model="editOpen"
      :title="`编辑题目「${editTarget?.ident ?? ''}」`"
      :submitting="saveProblem.pending.value"
      :disabled="!editIdent.trim()"
      confirm-text="保存"
      @submit="saveProblem.run(undefined)"
    >
      <el-form label-width="86px">
        <el-form-item label="题目标识">
          <el-input v-model="editIdent" maxlength="64" placeholder="例如 p1" />
        </el-form-item>
        <el-form-item label="标题">
          <el-input v-model="editTitle" maxlength="200" placeholder="例如 签到题" />
        </el-form-item>
        <el-form-item label="顺序">
          <el-input-number v-model="editOrder" :min="0" :max="9999" controls-position="right" />
        </el-form-item>
        <el-form-item label="代码路径">
          <el-input
            v-model="editPatterns"
            type="textarea"
            :rows="4"
            placeholder="每行一个，例如 {ident}/**"
          />
        </el-form-item>
      </el-form>
    </FormDialog>

    <!-- 删除题目：题目标识逐字确认 -->
    <ConfirmByNameDialog
      v-model="deleteOpen"
      :title="`删除题目「${deleteTarget?.ident ?? ''}」？`"
      :expected="deleteTarget?.ident ?? ''"
      :submitting="removeProblem.pending.value"
      confirm-text="删除"
      detail="将从清单删除该题，不可恢复；已有成绩保留，在成绩矩阵中显示为未登记。"
      @confirm="removeProblem.run(undefined)"
    />
    <ConfirmByNameDialog
      v-model="clearOpen"
      title="清空题目？"
      :expected="contestSlug"
      :submitting="clearProblems.pending.value"
      confirm-text="清空"
      :detail="`将删除本场次全部 ${problemCount ?? 0} 道题目，不可恢复；已有成绩保留。`"
      @confirm="clearProblems.run(undefined)"
    />
    <ProblemJudgeDialog v-model="judgeOpen" :problem="judgeTarget" @saved="problems.reload" />
  </el-dialog>
</template>

<style scoped>
.probe-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.pattern-tag {
  margin: 2px 4px 2px 0;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.preview {
  margin-top: 10px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.danger-text {
  color: var(--el-color-danger);
}

.bad-line {
  font-size: 12px;
  color: var(--el-color-danger);
}
</style>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { problemApi } from '@/api'
import type { ProblemMatchOut, ProblemOut, ProblemUpsert } from '@/api/types'
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

const problems = ref<ProblemOut[]>([])
const loading = ref(false)
const submitting = ref(false)
const raw = ref('')

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

async function load(): Promise<void> {
  if (!props.contestId) {
    problems.value = []
    return
  }
  loading.value = true
  try {
    problems.value = await problemApi.list(props.contestId)
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    loading.value = false
  }
}

watch(visible, (open) => {
  if (open) {
    raw.value = ''
    editingId.value = null
    probePath.value = ''
    probeResult.value = null
    void load()
  }
})

async function submit(): Promise<void> {
  if (!props.contestId) return
  if (!validRows.value.length) {
    ElMessage.warning('没有可登记的题目')
    return
  }

  submitting.value = true
  try {
    const payload: ProblemUpsert[] = validRows.value.map((row) => ({
      ident: row.ident,
      title: row.title || null,
      // 0 表示"让服务端按提交顺序自动编号"。
      // 注意这个字段不能省：openapi-typescript 会把"有非 None 默认值"的字段
      // 渲染成必填（服务端总会给值），省略它会直接编译不过。
      order_index: 0,
      file_patterns: row.patterns,
    }))
    const result = await problemApi.import(props.contestId, payload)
    // 服务端是逐条容错的：某一条不合法只会出现在 errors 里，其余照常入库
    if (result.errors?.length) {
      ElMessage.warning(`已登记 ${result.created} 道，${result.errors.length} 道被拒绝`)
      for (const message of result.errors.slice(0, 5)) {
        ElMessage.error(message)
      }
    } else {
      ElMessage.success(`新增 ${result.created} 道，更新 ${result.updated} 道`)
    }
    raw.value = ''
    await load()
    emit('changed')
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    submitting.value = false
  }
}

async function remove(problem: ProblemOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `从清单里删除题目「${problem.ident}」？\n\n` +
        '已有成绩记录会保留 —— 它们会以「未登记」的形式继续显示在成绩矩阵里。',
      '删除题目',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await problemApi.remove(problem.id)
    ElMessage.success('已删除')
    await load()
    emit('changed')
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

/** 就地编辑标题 / 顺序 / 代码路径模式。 */
const editingId = ref<number | null>(null)
const editTitle = ref('')
const editOrder = ref(0)
/** 一行一个模式；空 = 回到默认模式。 */
const editPatterns = ref('')

function startEdit(problem: ProblemOut): void {
  editingId.value = problem.id
  editTitle.value = problem.title ?? ''
  editOrder.value = problem.order_index ?? 0
  // 把**当前生效**的模式填进去（含服务端补的默认值），而不是原始存储值 ——
  // 否则教师看不到"现在到底按什么在认领文件"
  editPatterns.value = (problem.file_patterns ?? []).join('\n')
}

async function saveEdit(problem: ProblemOut): Promise<void> {
  const patterns = editPatterns.value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)

  try {
    await problemApi.update(problem.id, {
      ident: problem.ident,
      title: editTitle.value.trim() || null,
      order_index: editOrder.value,
      file_patterns: patterns,
    })
    editingId.value = null
    ElMessage.success('已保存')
    await load()
    emit('changed')
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
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

async function runProbe(): Promise<void> {
  const path = probePath.value.trim()
  if (!props.contestId || !path) return
  probing.value = true
  try {
    probeResult.value = await problemApi.match(props.contestId, path)
  } catch (err) {
    probeResult.value = null
    ElMessage.error((err as Error).message)
  } finally {
    probing.value = false
  }
}
</script>

<template>
  <el-dialog v-model="visible" title="题目清单" width="880px" :close-on-click-modal="false">
    <el-alert type="info" :closable="false" show-icon style="margin-bottom: 12px">
      <template #title>题目标识同时是目录名、代码文件名与成绩矩阵的列名</template>
      <template #default>
        默认约定结构：<code>桌面/&lt;准考证号&gt;/&lt;题目名&gt;/&lt;题目名&gt;.cpp</code>。
        所以标识要能安全用作目录名 —— 不要带 <code>/</code>、<code>..</code> 这类字符。
        <br />
        「代码路径」是按题目覆盖的 glob 模式，留空即用默认值 <code>{ident}/**</code>。
        通配符：<code>*</code> 不跨 <code>/</code>，<code>**</code> 跨，<code>?</code> 单字符，
        <code>[abc]</code> 字符类。
        <br />
        <code>{ident}</code> 与 <code>{title}</code> 是<strong>活占位符</strong>
        （每次匹配时展开），所以改了标识或标题不用回来改模式；标题留空时
        <code>{title}</code> 退回标识。
        顺序决定成绩矩阵的列序（不是字典序），也决定多条模式里谁先命中。
      </template>
    </el-alert>

    <el-table
      :data="problems"
      v-loading="loading"
      size="small"
      border
      max-height="280"
      style="margin-bottom: 16px"
    >
      <el-table-column label="顺序" width="90">
        <template #default="{ row }">
          <el-input-number
            v-if="editingId === row.id"
            v-model="editOrder"
            size="small"
            :min="0"
            :max="9999"
            controls-position="right"
            style="width: 80px"
          />
          <span v-else class="mono">{{ row.order_index }}</span>
        </template>
      </el-table-column>

      <el-table-column label="标识" width="130">
        <template #default="{ row }">
          <span class="mono">{{ row.ident }}</span>
        </template>
      </el-table-column>

      <el-table-column label="标题" width="150">
        <template #default="{ row }">
          <el-input
            v-if="editingId === row.id"
            v-model="editTitle"
            size="small"
            placeholder="显示标题（可留空）"
          />
          <span v-else>{{ row.title || '—' }}</span>
        </template>
      </el-table-column>

      <el-table-column label="代码路径" min-width="220">
        <template #default="{ row }">
          <el-input
            v-if="editingId === row.id"
            v-model="editPatterns"
            type="textarea"
            :rows="3"
            size="small"
            placeholder="一行一个模式，留空用默认值"
          />
          <template v-else>
            <el-tag
              v-for="pattern in row.file_patterns"
              :key="pattern"
              size="small"
              effect="plain"
              class="pattern-tag"
            >
              {{ pattern }}
            </el-tag>
            <span v-if="!row.file_patterns?.length" class="muted">（无）</span>
          </template>
        </template>
      </el-table-column>

      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <template v-if="editingId === row.id">
            <el-button link type="primary" size="small" @click="saveEdit(row)">保存</el-button>
            <el-button link size="small" @click="editingId = null">取消</el-button>
          </template>
          <template v-else>
            <el-button link type="primary" size="small" @click="startEdit(row)">编辑</el-button>
            <el-button link type="danger" size="small" @click="remove(row)">删除</el-button>
          </template>
        </template>
      </el-table-column>

      <template #empty>
        <div class="empty-block">还没有登记任何题目</div>
      </template>
    </el-table>

    <div class="probe-section">
      <div class="add-title">路径试算（确认模式配对了没有）</div>
      <div class="probe-row">
        <el-input
          v-model="probePath"
          size="small"
          placeholder="p1/p1.cpp"
          style="max-width: 380px"
          @keyup.enter="runProbe"
        />
        <el-button size="small" :loading="probing" @click="runProbe">试算</el-button>
        <template v-if="probeResult">
          <el-tag v-if="probeResult.problem" type="success" size="small">
            归到「{{ probeResult.problem }}」
          </el-tag>
          <el-tag v-else type="warning" size="small">没有任何题目认领这条路径</el-tag>
          <!-- 模板和展开结果都显示：教师写的是 {ident}/**，实际匹配的是 p1/** -->
          <span v-if="probeResult.expanded?.length" class="muted mono">
            命中：{{ (probeResult.patterns ?? []).join('  |  ') }}
            <template v-if="probeResult.patterns?.[0] !== probeResult.expanded[0]">
              → {{ probeResult.expanded.join('  |  ') }}
            </template>
          </span>
        </template>
      </div>
      <div class="page-hint">
        这是服务端算出来的结果，和回收文件时用的是同一份实现 —— 拿不准就在这里试，
        别对着模式猜。
      </div>
    </div>

    <div class="add-section">
      <div class="add-title">
        批量登记（每行一道：<code>标识,标题,代码路径</code>，后两列可省略）
      </div>
      <el-input
        v-model="raw"
        type="textarea"
        :rows="6"
        placeholder="p1,签到题&#10;p2,图论,{ident}/**;src/**&#10;p3"
      />

      <div v-if="parsed.length" class="preview">
        <el-tag type="success" size="small" effect="plain">
          {{ validRows.length }} 道可登记
        </el-tag>
        <el-tag
          v-if="invalidRows.length"
          type="danger"
          size="small"
          effect="plain"
          style="margin-left: 6px"
        >
          {{ invalidRows.length }} 道有问题（会被跳过）
        </el-tag>
        <span v-for="row in invalidRows.slice(0, 5)" :key="row.line" class="bad-line">
          第 {{ row.line }} 行：{{ row.error }}
        </span>
        <span v-if="validRows.some((row) => row.patterns.length)" class="muted">
          带自定义代码路径的题目会用配的模式认领文件，其余用服务端默认值。
        </span>
      </div>

      <el-button
        type="primary"
        :loading="submitting"
        :disabled="!validRows.length"
        style="margin-top: 10px"
        @click="submit"
      >
        登记 {{ validRows.length }} 道
      </el-button>
      <span class="page-hint" style="margin-left: 10px">
        相同标识会更新已有题目，所以这份清单可以反复导。
      </span>
    </div>
  </el-dialog>
</template>

<style scoped>
.add-section {
  border-top: 1px solid #ebeef5;
  padding-top: 14px;
}

.probe-section {
  border-top: 1px solid #ebeef5;
  padding-top: 12px;
  margin-bottom: 12px;
}

.probe-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 6px;
}

.pattern-tag {
  margin: 2px 4px 2px 0;
}

.muted {
  color: #909399;
  font-size: 12px;
}

.add-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 8px;
}

.preview {
  margin-top: 10px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.bad-line {
  font-size: 12px;
  color: #f56c6c;
}
</style>

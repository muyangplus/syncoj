<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessageBox } from 'element-plus'

import { assetApi, deployApi, playerApi, problemApi } from '@/api'
import type { AssetOut, DeployTaskOut, PlayerOut, ProblemOut } from '@/api/types'
import DataTable from '@/components/DataTable.vue'
import FormDialog from '@/components/FormDialog.vue'
import HelpTip from '@/components/HelpTip.vue'
import PageShell from '@/components/PageShell.vue'
import { useList } from '@/composables/useList'
import { useMutation } from '@/composables/useMutation'
import { useContestStore } from '@/stores/contest'
import { formatBytes, formatTime, percent } from '@/utils/format'

const contest = useContestStore()

// --------------------------------------------------------------------------- //
// 两份列表
//
// 一页上有两种东西：可下发的资产、下发任务。它们各自分页、各自轮询 ——
// 用两个 `useList` 而不是合成一个"页面数据"对象，是因为任务的进度每 10 秒
// 都在变、资产却只在教师上传/改名时变；合成一份会让资产的翻页跟着任务的
// 轮询一起跳。两份列表都靠 `watches` 在场次切换时回到第一页重拉。
// --------------------------------------------------------------------------- //

const assets = useList<AssetOut>({
  rowKey: (row) => row.id,
  loader: ({ limit, offset, signal }) => {
    const contestId = contest.currentId
    // 还没选场次时返回 null：列表空着但不报错 —— "没选场次"不是错误
    if (!contestId) return Promise.resolve(null)
    return assetApi.list(contestId, { limit, offset }, signal)
  },
  // 资产是"几十个"量级的集合，一次取完：新建下发时的下拉要用到全部，
  // 分页取的话教师会以为"我上传的文件少了"
  pageSize: 500,
  watches: [() => contest.currentId],
})

const tasks = useList<DeployTaskOut>({
  rowKey: (row) => row.id,
  loader: ({ limit, offset, signal }) => {
    const contestId = contest.currentId
    if (!contestId) return Promise.resolve(null)
    // 带上逐选手目标：进度条和"哪台卡住了"都要靠它。任务数比选手数少得多，
    // 所以整页带回来反而比"每行展开再单独拉一次"更省事
    return deployApi.list(contestId, { limit, offset, includeTargets: true }, signal)
  },
  // 每条任务带着全部目标，比资产重，页小一点
  pageSize: 20,
  // 10 秒一轮：教师刚发了文件，会盯着这一页看进度
  interval: 10000,
  watches: [() => contest.currentId],
})

/** 两份列表任一出错就挂在页面顶部 —— 分成两条红字只会让人以为页面向下多了一块。 */
const listError = computed(() => assets.error.value ?? tasks.error.value)

async function refreshAll(): Promise<void> {
  await Promise.all([assets.reload(), tasks.reload()])
}

// --------------------------------------------------------------------------- //
// 下拉选项：选手与题目
//
// 它们只用来填"下发给谁 / 按哪道题"两个选择框，不进表格，所以不值得各起一个
// `useList`；而且它们拉不到时不该把主列表也弄成错误状态 —— 主列表自己会报它的问题。
// --------------------------------------------------------------------------- //

const players = ref<PlayerOut[]>([])
const problems = ref<ProblemOut[]>([])

async function loadOptions(): Promise<void> {
  const contestId = contest.currentId
  if (!contestId) {
    players.value = []
    problems.value = []
    return
  }
  try {
    const [playerPage, problemPage] = await Promise.all([
      playerApi.list(contestId, { limit: 500 }),
      problemApi.list(contestId, { limit: 500 }),
    ])
    players.value = playerPage.items
    problems.value = problemPage.items
  } catch {
    players.value = []
    problems.value = []
  }
}

void loadOptions()
watch(() => contest.currentId, () => void loadOptions())

const groups = computed(() => {
  const set = new Set<string>()
  for (const player of players.value) {
    if (player.group_name) set.add(player.group_name)
  }
  return [...set]
})

// --------------------------------------------------------------------------- //
// 上传
//
// 上传的是**文件本身**，服务端原样落盘、不解压。所以题面与样例都应该在教师
// 自己的机器上先打包成 zip 再上传 —— 传一个文件夹进来只会变成一个打不开的
// 条目，学生那边也恢复不出目录结构。
// --------------------------------------------------------------------------- //

/** 服务端默认值是 `testdata`，界面上给个中文名；教师自己填的照原样显示。 */
const KIND_ALIASES: Record<string, string> = {
  testdata: '测试点包',
  statement: '题面',
  sample: '样例',
}

function kindLabel(kind: string): string {
  return KIND_ALIASES[kind] ?? kind
}

const KIND_PRESETS = ['题面', '样例', '测试点', '须知', '其他']
const uploadKind = ref('题面')
const fileInput = ref<HTMLInputElement>()

const upload = useMutation(
  async (file: File) => {
    const contestId = contest.currentId
    if (!contestId) throw new Error('还没有选场次')
    return assetApi.upload(contestId, file, uploadKind.value)
  },
  {
    success: (asset) => `已上传 ${asset.filename}（${formatBytes(asset.size)}）`,
    onDone: () => assets.reload(),
  },
)

function pickFile(): void {
  fileInput.value?.click()
}

function handleFileChange(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = '' // 允许连续上传同一个文件
  if (!file) return
  void upload.run(file)
}

// --------------------------------------------------------------------------- //
// 在界面里直接写一个纯文本资产
//
// 落盘后它与上传的资产**完全一样**（同一套内容寻址、同一条下发流程），之所以
// 还要这个入口，是因为"比赛结束后不要关机"这句话本来就是在浏览器里打的 ——
// 为了发它先在自己机器上造一个文件再传上来，是纯粹的仪式。
//
// 考场公告是它的一个特例：文件名固定是 `NOTICE.md`，选手页会把它显示成
// 「考场公告」。所以这里只多一个预填的名字，不另开一条代码路径。
// --------------------------------------------------------------------------- //

/** 选手页「考场公告」认的就是这个文件名。 */
const NOTICE_FILENAME = 'NOTICE.md'

const textDialog = ref(false)
const textForm = reactive({ filename: '', content: '' })

/** 标题与提示都跟着文件名走：它是不是考场公告，看的就是这个名字。 */
const textIsNotice = computed(() => textForm.filename.trim() === NOTICE_FILENAME)

/**
 * 两个入口共用一个对话框，区别只有预填的文件名。
 *
 * 复制成两份表单的话，"内容可以为空"这类规则迟早只在其中一份里改 ——
 * 而它们本来就该是同一个动作。
 */
function openTextDialog(filename = ''): void {
  textForm.filename = filename
  textForm.content = ''
  // 「写考场公告」把用途预置成「须知」：kind 决定它落到机器上的目标目录，
  // 一份 NOTICE.md 落在「题面」目录里是脏的。对话框里的 select 绑的是这同一个
  // ref，所以教师看得见、也还能改；「新建文本文件」保持他当前选的那一个。
  if (filename.trim() === NOTICE_FILENAME) uploadKind.value = '须知'
  textDialog.value = true
}

const createText = useMutation(
  async () => {
    const contestId = contest.currentId
    if (!contestId) throw new Error('还没有选场次')
    const filename = textForm.filename.trim()
    if (!filename) throw new Error('文件名不能为空')
    // 提交前先拍一份快照。服务端对"同场次 + 同名 + 同内容"返回的是**已存在的
    // 那一条**（id 与之前相同），所以拿返回的 id 跟这份快照比，就能分清这次是
    // 新建还是复用 —— 一律报"新建成功"会让教师以为生成了两份。
    //
    // 快照只是**当前这一页**：列表被截断时（资产超过一页）还有第三种可能 ——
    // 复用的是更早那一页上的那条。那时不去额外拉一页确认，也不撒谎，只说"已保存"。
    const knownIds = new Set(assets.rows.value.map((row) => row.id))
    const truncated = assets.total.value > assets.rows.value.length
    const asset = await assetApi.createText(contestId, {
      filename,
      // 用途沿用工具栏那一套选项：同一个词，上传和手写没有理由用两个
      kind: uploadKind.value,
      content: textForm.content,
    })
    return { asset, reused: knownIds.has(asset.id), truncated }
  },
  {
    success: ({ asset, reused, truncated }) => {
      if (reused) return `已存在同名同内容的资产「${asset.filename}」，没有重复创建`
      // 列表没看全时不敢断言"新建"：命中更早那一页上的旧资产同样是合法的。
      // 两种情况的最终状态完全一样（不会造出两份），差的只是收据的措辞。
      const verb = truncated ? '已保存' : '已新建'
      return `${verb}文本文件「${asset.filename}」（${formatBytes(asset.size)}）`
    },
    onDone: async () => {
      textDialog.value = false
      await assets.reload()
    },
  },
)

// --------------------------------------------------------------------------- //
// 资产：改名与删除
// --------------------------------------------------------------------------- //

const renameOpen = ref(false)
const renameTarget = ref<AssetOut | null>(null)
const renameName = ref('')

function openRename(asset: AssetOut): void {
  renameTarget.value = asset
  renameName.value = asset.filename
  renameOpen.value = true
}

/**
 * 改名只是换标签。
 *
 * 内容是按 sha256 存的内容寻址对象，改名不碰内容；已经落到选手机器上的文件
 * 也早就落地了，不受影响。但**未完成**的下发任务会按新名字落地 —— 不说这句
 * 的话，教师会以为改名能修正已经发出去的文件。
 */
const renameAsset = useMutation(
  async () => {
    const asset = renameTarget.value
    if (!asset) throw new Error('没有选中文件')
    const name = renameName.value.trim()
    if (!name) throw new Error('文件名不能为空')
    // 服务端会按路径段的规则校验，这里先挡一次明显的错，省一个来回
    if (name.includes('/') || name.includes('\\')) {
      throw new Error('文件名不能包含斜杠 —— 它只是一个文件名，不是路径')
    }
    return assetApi.rename(asset.id, { filename: name })
  },
  {
    success: (asset) =>
      `已改名为「${asset.filename}」。内容没动，已经落到机器上的那份不受影响；还没下完的任务会按新名字落地`,
    onDone: async () => {
      renameOpen.value = false
      renameTarget.value = null
      await assets.reload()
    },
  },
)

/**
 * 删除资产是**软删除**（墓碑），所以用普通确认而不是打名字 —— 约定里的
 * "打一遍名字"是给结构性数据的硬删除用的。
 *
 * 但后果必须写清楚：服务端会级联删掉引用它的下发任务与逐选手进度，
 * 而已经落到选手机器上的文件**不会撤回**（服务端管不到那边）。
 */
const removeAsset = useMutation((asset: AssetOut) => assetApi.remove(asset.id), {
  onDone: () => assets.reload(),
})

async function askRemoveAsset(asset: AssetOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
        '引用它的下发任务与逐选手进度会一起被删掉。\n' +
        '已经落到选手机器上的文件不会撤回。',
      '删除可下发文件',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await removeAsset.run(asset)
}

// --------------------------------------------------------------------------- //
// 新建下发任务
// --------------------------------------------------------------------------- //

const createDialog = ref(false)

/**
 * 目标目录模板。
 *
 * `{player_no}` 由**服务端**逐选手展开 —— 全员下发时每台机器的目标目录都
 * 不同（`桌面/<各自的准考证号>/…`），这个替换客户端做不了（它不知道这次
 * 下发是给谁的）。
 *
 * 默认值是**空串，也就是桌面根目录**：最常见的一次下发是题面 zip + 样例 zip，
 * 它们就该直接躺在桌面上，选手双击解压就能看。只有"按题分发的附件"才需要
 * `{player_no}/<题目名>` —— 那种情况选了题目会自动填上。
 *
 * agent 侧的 `deploy_root` 默认就是桌面，所以这里的相对路径从桌面往下写。
 */
const form = reactive({
  assetId: undefined as number | undefined,
  targetKind: 'all' as 'all' | 'player' | 'group',
  playerIds: [] as number[],
  targetGroup: '',
  problemIdent: '',
  destDir: '',
  mode: 'overwrite' as 'overwrite' | 'skip_exist',
})

/**
 * 目标目录的实时预览。
 *
 * 模板里有占位符，教师看到 `{player_no}/p1` 不一定想得到最终落到哪。
 * 这里把第一个目标选手代入算给他看 —— 比写一段说明文字有用得多。
 */
const destPreview = computed(() => {
  const probe = players.value[0]?.player_no ?? 'S001'
  return form.destDir.trim().replace(/\{player_no\}/g, probe) || '（桌面根目录）'
})

/** 选题时自动把题目名拼进目标目录 —— 教师不用手打。 */
function applyProblem(ident: string): void {
  form.problemIdent = ident
  // 选了题目 = 按题分发（附件放该选手的题目目录下）；
  // 不选 = 通用资料（题面 zip、样例 zip、须知），直接放桌面根目录
  form.destDir = ident ? `{player_no}/${ident}` : ''
}

function openCreate(asset?: AssetOut): void {
  form.assetId = asset?.id
  form.targetKind = 'all'
  form.playerIds = []
  form.targetGroup = groups.value[0] ?? ''
  // 默认落到**桌面根目录** —— 题面 zip、样例 zip、须知这类东西就该直接摆在
  // 桌面上。要按题分发附件时，选中题目会自动改成 `{player_no}/<题目名>`。
  form.problemIdent = ''
  form.destDir = ''
  form.mode = 'overwrite'
  createDialog.value = true
}

const createDeploy = useMutation(
  async () => {
    const contestId = contest.currentId
    if (!contestId) throw new Error('还没有选场次')
    if (!form.assetId) throw new Error('请选择要下发的文件')
    if (form.targetKind === 'player' && form.playerIds.length === 0) {
      throw new Error('请选择至少一名选手')
    }
    if (form.targetKind === 'group' && !form.targetGroup) {
      throw new Error('请选择分组')
    }
    return deployApi.create(contestId, {
      asset_id: form.assetId,
      target_kind: form.targetKind,
      player_ids: form.playerIds,
      target_group: form.targetKind === 'group' ? form.targetGroup : null,
      // 服务端会剥掉末尾斜杠，这里原样传即可
      dest_dir: form.destDir.trim(),
      mode: form.mode,
    })
  },
  {
    // `DeployTaskOut` 上没有服务端写的回执，只能自己拼一句；目标数是这套动作里
    // 教师最想立刻看到的数字
    success: (task) => `已创建下发任务，目标 ${task.total} 台`,
    onDone: async () => {
      createDialog.value = false
      await tasks.reload()
    },
  },
)

// --------------------------------------------------------------------------- //
// 取消、重试、删除下发任务
// --------------------------------------------------------------------------- //

const cancelTask = useMutation((task: DeployTaskOut) => deployApi.cancel(task.id), {
  onDone: () => tasks.reload(),
})

async function askCancel(task: DeployTaskOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `取消下发「${task.filename}」？\n\n` +
        '只停掉还没完成的目标 —— 已经收到文件的机器不会回滚。',
      '取消下发',
      { type: 'warning', confirmButtonText: '取消下发', cancelButtonText: '再想想' },
    )
  } catch {
    return
  }
  await cancelTask.run(task)
}

const retryTask = useMutation((task: DeployTaskOut) => deployApi.retry(task.id), {
  onDone: () => tasks.reload(),
})

/**
 * 删除一条下发记录。它是动作日志、不是结构性数据（删掉不会伤到名单或场次），
 * 而且没有"一个名字"值得打一遍 —— 所以用一条把后果写清楚的普通确认就够了。
 *
 * 服务端会顺手把还没下完的台数写进回执（`SimpleAck.detail`），所以这里不必
 * 自己数 —— 那句"有 N 台还没下完"比界面编的"已删除"有用。
 */
const removeTask = useMutation((task: DeployTaskOut) => deployApi.remove(task.id), {
  onDone: () => tasks.reload(),
})

async function askRemoveTask(task: DeployTaskOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `删除下发记录「${task.filename}」？\n\n` +
        '已经落地的文件不会撤回；还没下完的机器不会再收到这个作业。\n' +
        '要停止下发请用「取消」。',
      '删除下发记录',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await removeTask.run(task)
}

// --------------------------------------------------------------------------- //
// 展示用的小工具
// --------------------------------------------------------------------------- //

function statusType(status: string): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'done') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'cancelled') return 'info'
  return 'warning'
}

function statusLabel(status: string): string {
  const map: Record<string, string> = {
    pending: '进行中',
    ready: '进行中',
    done: '已完成',
    failed: '有失败',
    cancelled: '已取消',
  }
  return map[status] ?? status
}

function targetKindLabel(kind: string): string {
  if (kind === 'all') return '全员'
  if (kind === 'group') return '按分组'
  return '指定选手'
}
</script>

<template>
  <PageShell
    title="文件下发"
    hint="题面和样例都当 zip 文件上传，系统不解压。"
    :error="listError"
    error-action="刷新页面或检查服务端；资产列表取不到时不要下发。"
    retryable
    @retry="refreshAll"
  >
    <template #hint>
      <HelpTip>
        带密码的题面：把 <code>password.txt</code> 当普通资产一起下发即可。
      </HelpTip>
    </template>
    <template #toolbar>
      <input ref="fileInput" type="file" style="display: none" @change="handleFileChange" />
      <span class="field-label">类别</span>
      <el-select v-model="uploadKind" size="small" style="width: 110px">
        <el-option v-for="kind in KIND_PRESETS" :key="kind" :label="kind" :value="kind" />
      </el-select>
      <el-button size="small" :loading="upload.pending.value" @click="pickFile">上传文件</el-button>
      <el-button size="small" @click="openTextDialog()">新建文本文件</el-button>
      <el-button size="small" @click="openTextDialog(NOTICE_FILENAME)">写考场公告</el-button>
      <el-button
        size="small"
        type="primary"
        :disabled="!assets.total.value"
        @click="openCreate()"
      >
        新建下发
      </el-button>
      <el-button size="small" :loading="assets.loading.value || tasks.loading.value" @click="refreshAll">
        刷新
      </el-button>
    </template>

    <div class="list-head">
      <h3 class="section-title">
        可下发文件
        <span class="muted">（共 {{ assets.total.value }} 个）</span>
      </h3>
    </div>

    <DataTable
      :rows="assets.rows.value"
      :row-key="(row: AssetOut) => row.id"
      :loading="assets.loading.value"
      :total="assets.total.value"
      :page="assets.page.value"
      :page-size="assets.pageSize.value"
      empty-text="还没有上传任何文件"
      @update:page="assets.setPage"
      @update:pageSize="assets.setPageSize"
    >
      <template #columns>
        <el-table-column label="文件名" min-width="200">
          <template #default="{ row }">
            <span class="mono">{{ row.filename }}</span>
            <!-- 不会自己解压这件事已经写在页面标题的问号里，这里只标出来它是压缩包 -->
            <el-tag v-if="row.filename.toLowerCase().endsWith('.zip')" size="small" effect="plain">
              压缩包
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="类别" width="110">
          <template #default="{ row }">
            <span class="cell-sub">{{ kindLabel(row.kind) }}</span>
          </template>
        </el-table-column>

        <!-- 精确字节数：核对"传上去的是不是手上这一份"时看的就是它 -->
        <el-table-column label="大小" width="110" align="right">
          <template #default="{ row }">
            <el-tooltip :content="`${row.size.toLocaleString()} 字节`">
              <span class="mono">{{ formatBytes(row.size) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="内容哈希" width="140">
          <template #default="{ row }">
            <el-tooltip :content="row.sha256">
              <span class="mono muted">{{ row.sha256.slice(0, 12) }}…</span>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="上传时间" width="160">
          <template #default="{ row }">
            <span class="cell-sub">{{ formatTime(row.created_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="170" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openCreate(row)">下发</el-button>
            <el-button link type="primary" size="small" @click="openRename(row)">改名</el-button>
            <el-button link type="danger" size="small" @click="askRemoveAsset(row)">删除</el-button>
          </template>
        </el-table-column>
      </template>

      <template #empty>
        <p>还没有上传任何文件。</p>
        <p>先在本地打包成 zip 再传。</p>
        <el-button type="primary" size="small" style="margin-top: 12px" @click="pickFile">
          上传文件
        </el-button>
      </template>
    </DataTable>

    <div class="list-head" style="margin-top: 22px">
      <h3 class="section-title">
        下发任务
        <span class="muted">（共 {{ tasks.total.value }} 条）</span>
      </h3>
      <p class="page-hint" style="margin: 0">展开一行看每一台机器的情况。</p>
    </div>

    <DataTable
      :rows="tasks.rows.value"
      :row-key="(row: DeployTaskOut) => row.id"
      :loading="tasks.loading.value"
      :total="tasks.total.value"
      :page="tasks.page.value"
      :page-size="tasks.pageSize.value"
      empty-text="还没有任何下发任务"
      @update:page="tasks.setPage"
      @update:pageSize="tasks.setPageSize"
    >
      <template #columns>
        <!-- 逐选手进度：`include_targets=true` 已经把它一起带回来了，不用展开再拉 -->
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="expand-body">
              <el-table v-if="row.targets?.length" :data="row.targets" size="small" border>
                <el-table-column label="选手" prop="player_no" width="110" />
                <el-table-column label="状态" width="100">
                  <template #default="scope">
                    <el-tag :type="statusType(scope.row.status)" size="small" effect="plain">
                      {{ statusLabel(scope.row.status) }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="该机进度" min-width="160">
                  <template #default="scope">
                    <el-progress
                      :percentage="percent(scope.row.bytes_done, row.size)"
                      :status="scope.row.status === 'failed' ? 'exception' : undefined"
                      :stroke-width="10"
                    />
                  </template>
                </el-table-column>
                <el-table-column label="已传字节" width="120" align="right">
                  <template #default="scope">
                    <el-tooltip :content="`共 ${formatBytes(row.size)}`">
                      <span class="mono">{{ scope.row.bytes_done.toLocaleString() }}</span>
                    </el-tooltip>
                  </template>
                </el-table-column>
                <el-table-column label="重试" prop="retry_count" width="70" align="right" />
                <el-table-column label="错误" min-width="240">
                  <template #default="scope">
                    <span v-if="scope.row.last_error" class="error-text">
                      {{ scope.row.last_error }}
                    </span>
                    <span v-else class="muted">—</span>
                  </template>
                </el-table-column>
              </el-table>
              <!-- 一条目标都没有：任务建出来时目标为空，或者解析不出选手 -->
              <p v-else class="page-hint">这条任务没有任何目标。</p>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="文件" min-width="180">
          <template #default="{ row }">
            <span class="mono">{{ row.filename }}</span>
            <div class="cell-sub">{{ formatBytes(row.size) }}</div>
          </template>
        </el-table-column>

        <el-table-column label="目标" width="180">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ targetKindLabel(row.target_kind) }}</el-tag>
            <div class="cell-sub">目录：{{ row.dest_dir || '（桌面根目录）' }}</div>
            <div class="cell-sub">同名文件：{{ row.mode === 'skip_exist' ? '跳过' : '覆盖' }}</div>
          </template>
        </el-table-column>

        <el-table-column label="进度" min-width="220">
          <template #default="{ row }">
            <el-progress
              :percentage="percent(row.done + row.failed, row.total)"
              :status="row.failed > 0 ? 'exception' : row.status === 'done' ? 'success' : undefined"
              :stroke-width="14"
            />
            <div class="cell-sub">
              完成 {{ row.done }} · 失败 {{ row.failed }} · 待处理 {{ row.pending }} / 共 {{ row.total }}
            </div>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small" effect="plain">
              {{ statusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="创建时间" width="160">
          <template #default="{ row }">
            <span class="cell-sub">{{ formatTime(row.created_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="row.status !== 'done' && row.status !== 'cancelled'"
              link
              type="danger"
              size="small"
              :loading="cancelTask.pending.value"
              @click="askCancel(row)"
            >
              取消
            </el-button>
            <el-button
              v-if="row.failed > 0"
              link
              type="primary"
              size="small"
              :loading="retryTask.pending.value"
              @click="retryTask.run(row)"
            >
              重试
            </el-button>
            <el-button link type="danger" size="small" @click="askRemoveTask(row)">删除</el-button>
          </template>
        </el-table-column>
      </template>

      <template #empty>
        <p>还没有任何下发任务。</p>
        <p>先在上面「上传文件」，再点它那一行的「下发」。</p>
        <el-button type="primary" size="small" style="margin-top: 12px" @click="pickFile">
          上传文件
        </el-button>
      </template>
    </DataTable>

    <FormDialog
      v-model="createDialog"
      title="新建下发任务"
      :submitting="createDeploy.pending.value"
      confirm-text="创建"
      @submit="createDeploy.run(undefined)"
    >
      <el-form label-width="90px">
        <el-form-item label="下发文件">
          <el-select v-model="form.assetId" placeholder="选择文件" style="width: 100%">
            <el-option
              v-for="asset in assets.rows.value"
              :key="asset.id"
              :label="`${asset.filename}（${formatBytes(asset.size)}）`"
              :value="asset.id"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="下发范围">
          <el-radio-group v-model="form.targetKind">
            <el-radio-button value="all">全员</el-radio-button>
            <el-radio-button value="group">按分组</el-radio-button>
            <el-radio-button value="player">指定选手</el-radio-button>
          </el-radio-group>
        </el-form-item>

        <el-form-item v-if="form.targetKind === 'group'" label="分组">
          <el-select v-model="form.targetGroup" placeholder="选择分组" style="width: 100%">
            <el-option v-for="group in groups" :key="group" :label="group" :value="group" />
          </el-select>
          <div v-if="!groups.length" class="page-hint">
            还没有任何选手设置了分组，请先在导入选手时填写 group_name。
          </div>
        </el-form-item>

        <el-form-item v-if="form.targetKind === 'player'" label="选手">
          <el-select
            v-model="form.playerIds"
            multiple
            filterable
            placeholder="选择选手"
            style="width: 100%"
          >
            <el-option
              v-for="player in players"
              :key="player.id"
              :label="`${player.player_no}${player.name ? ' ' + player.name : ''}`"
              :value="player.id"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="所属题目">
          <el-select
            v-model="form.problemIdent"
            placeholder="可选：选了就按题分发到该选手的题目目录"
            clearable
            style="width: 100%"
            @change="applyProblem"
          >
            <el-option
              v-for="problem in problems"
              :key="problem.id"
              :label="problem.title ? `${problem.title}（${problem.ident}）` : problem.ident"
              :value="problem.ident"
            />
          </el-select>
          <div class="page-hint">
            通用资料（题面 zip、样例 zip、须知）<strong>不用选</strong>，留空即落到桌面根目录。
          </div>
        </el-form-item>

        <el-form-item label="目标目录">
          <el-input
            v-model="form.destDir"
            placeholder="留空 = 桌面根目录；{player_no}/题目名 = 该选手的题目目录"
          />
          <div class="page-hint">
            <strong>留空就是桌面根目录</strong>；按题分发填
            <code>{player_no}/&lt;题目名&gt;</code>，选了题目会自动填好。
            <br />
            <strong>实际落点预览：</strong>
            <code>{{ destPreview }}</code>
          </div>
        </el-form-item>

        <el-form-item label="同名文件">
          <el-radio-group v-model="form.mode">
            <el-radio value="overwrite">覆盖</el-radio>
            <el-radio value="skip_exist">已存在则跳过</el-radio>
          </el-radio-group>
          <div class="page-hint">
            目标位置上已有同名文件时：覆盖会换掉它，跳过则留原来那份。
            「同名」指的是整个 zip 的名字。
          </div>
        </el-form-item>
      </el-form>
    </FormDialog>

    <FormDialog
      v-model="renameOpen"
      title="重命名可下发文件"
      :submitting="renameAsset.pending.value"
      confirm-text="改名"
      @submit="renameAsset.run(undefined)"
    >
      <el-form label-width="90px">
        <el-form-item label="文件名">
          <el-input v-model="renameName" placeholder="例如 题面.zip" maxlength="255" />
        </el-form-item>
      </el-form>
      <el-alert type="warning" :closable="false" show-icon>
      <template #title>只改显示名</template>
      <template #default>
        文件内容按 sha256 存，改名只换标签，已经落地的那份不受影响。
        <strong>还没下完的任务会按新名字落地。</strong>
      </template>
    </el-alert>
    </FormDialog>

    <!-- 「新建文本文件」与「写考场公告」共用这一个对话框，区别只有预填的文件名 -->
    <FormDialog
      v-model="textDialog"
      :title="textIsNotice ? '写考场公告' : '新建文本文件'"
      :submitting="createText.pending.value"
      :disabled="!textForm.filename.trim()"
      confirm-text="创建"
      @submit="createText.run(undefined)"
    >
      <el-alert
        v-if="textIsNotice"
        type="info"
        :closable="false"
        show-icon
        style="margin-bottom: 14px"
      >
        <template #title>
          这个文件会显示在选手页的「考场公告」里 —— 每个选手看到的是本场下发给他自己的那份。
        </template>
      </el-alert>

      <el-form label-width="90px">
        <el-form-item label="文件名" required>
          <el-input v-model="textForm.filename" placeholder="例如 须知.txt" maxlength="255" />
        </el-form-item>

        <el-form-item label="用途">
          <el-select v-model="uploadKind" style="width: 100%">
            <el-option v-for="kind in KIND_PRESETS" :key="kind" :label="kind" :value="kind" />
          </el-select>
        </el-form-item>

        <el-form-item label="内容">
          <el-input
            v-model="textForm.content"
            class="text-content"
            type="textarea"
            :rows="14"
            maxlength="200000"
            placeholder="可以留空 —— 留空就是一个空文件"
          />
        </el-form-item>
      </el-form>
    </FormDialog>
  </PageShell>
</template>

<style scoped>
.section-title {
  font-size: 15px;
  margin: 22px 0 10px;
}

.list-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.list-head .section-title {
  margin-bottom: 10px;
}

.field-label {
  font-size: 13px;
  color: #606266;
}

.muted {
  font-size: 12px;
  font-weight: 400;
  color: #909399;
}

.expand-body {
  padding: 8px 0 4px 42px;
}

.error-text {
  color: #f56c6c;
  font-size: 12px;
}

/* 正文往往是写给选手照着抄的（一行命令、一个路径），等宽字体比默认字体好抄 */
.text-content :deep(textarea) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace;
}
</style>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import { assetApi, deployApi, playerApi } from '@/api'
import { ApiError } from '@/api/client'
import type { AssetOut, AssetZipPasswordOut, DeployTaskOut, PlayerOut } from '@/api/types'
import DataTable from '@/components/DataTable.vue'
import FormDialog from '@/components/FormDialog.vue'
import PageShell from '@/components/PageShell.vue'
import SectionCard from '@/components/SectionCard.vue'
import { useList } from '@/composables/useList'
import { useMutation } from '@/composables/useMutation'
import { useContestStore } from '@/stores/contest'
import { formatBytes, formatTime, percent } from '@/utils/format'

const contest = useContestStore()
const route = useRoute()
const router = useRouter()

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
// 下拉选项：选手
//
// 只用来填"下发给谁"这个选择框，不进表格，所以不值得另起一个 `useList`；
// 而且它拉不到时不该把主列表也弄成错误状态 —— 主列表自己会报它的问题。
// --------------------------------------------------------------------------- //

const players = ref<PlayerOut[]>([])

async function loadOptions(): Promise<void> {
  const contestId = contest.currentId
  if (!contestId) {
    players.value = []
    return
  }
  try {
    const playerPage = await playerApi.list(contestId, { limit: 500 })
    players.value = playerPage.items
  } catch {
    players.value = []
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
// 默认上传的是**文件本身**：服务端原样落盘、不解压。题面与样例本来就该是 zip，
// 传一个文件夹进来只会变成一条打不开的条目，学生那边也恢复不出目录结构。
//
// 勾上「打包成 zip」之后反过来：服务端当场把字节包成 zip（成员名 = 原文件名，
// 资产名 = <原基名>.zip）再落盘。密码留空 = 服务端生成一个，之后只能在
// password.txt 里读到 —— 所以上传完还得把它下发一次，否则学生手里没有口令。
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

/** 上传时就让服务端打包成 zip（可选加密）。密码留空 = 服务端生成一个。 */
const uploadPackage = ref(false)
const uploadZipPassword = ref('')

const upload = useMutation(
  async (file: File) => {
    const contestId = contest.currentId
    if (!contestId) throw new Error('未选择场次')
    // 带走这一份快照：提交过程中教师可能又改了勾选，而回执说的是这次**实际**
    // 发生的事（服务端只认收到的字段）
    const packaged = uploadPackage.value
    const asset = await assetApi.upload(contestId, file, uploadKind.value, {
      packageZip: packaged,
      zipPassword: uploadZipPassword.value,
    })
    return { asset, packaged }
  },
  {
    success: ({ asset, packaged }) =>
      packaged
        ? `已打包为「${asset.filename}」（${formatBytes(asset.size)}），密码已写入 password.txt`
        : `已上传「${asset.filename}」（${formatBytes(asset.size)}）`,
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

/**
 * 这个对话框服务的三种事。
 *
 * 「新建」「写公告」「改已有资产的正文」共用同一份表单 —— 复制成三份的话，
 * "内容可以为空"这类规则迟早只在其中一份里改，而它们本来就是同一个动作
 * （写一段文字、存成一份下发文件）。区别只有：标题、预填的文件名、提交时打哪个接口。
 */
type TextMode = 'create' | 'notice' | 'edit'
const textMode = ref<TextMode>('create')

/** 正在改的那一条（`edit` 模式才有）。提交时要拿它的 id 去打 PUT。 */
const textTarget = ref<AssetOut | null>(null)

/**
 * 当前这一条能不能在线上改正文（服务端算的 `AssetOut.editable`）。
 *
 * 编辑对话框现在也承担**改名**，而改名对所有资产都成立；只有正文那一栏跟着这个
 * 布尔显示。前端不按扩展名自己判断 —— 两处规则必然分叉，分叉那天的表现是
 * "给了输入框、点保存报错"。
 */
const textEditable = ref(true)

/** 打开时先 GET 一次正文，那一次请求也要占着确认按钮，不然能连点两次。 */
const textLoading = ref(false)

/** 标题与提示都跟着文件名走：它是不是考场公告，看的就是这个名字。 */
const textIsNotice = computed(() => textForm.filename.trim() === NOTICE_FILENAME)

const textTitle = computed(() => {
  if (textMode.value === 'edit') return `编辑「${textTarget.value?.filename ?? '文件'}」`
  return textIsNotice.value ? '写考场公告' : '新建文本文件'
})

/**
 * 两个入口共用一个对话框，区别只有预填的文件名。
 *
 * 复制成两份表单的话，"内容可以为空"这类规则迟早只在其中一份里改 ——
 * 而它们本来就该是同一个动作。
 */
function openTextDialog(mode: TextMode, filename = ''): void {
  textMode.value = mode
  textTarget.value = null
  // 新建/写公告都是文本入口，正文那一栏一定要显示
  textEditable.value = true
  textForm.filename = filename
  textForm.content = ''
  // 「写考场公告」把用途预置成「须知」：kind 决定它落到机器上的目标目录，
  // 一份 NOTICE.md 落在「题面」目录里是脏的。对话框里的 select 绑的是这同一个
  // ref，所以教师看得见、也还能改；「新建文本文件」保持他当前选的那一个。
  if (filename.trim() === NOTICE_FILENAME) uploadKind.value = '须知'
  textDialog.value = true
}

/**
 * 打开一条已有资产的编辑对话框。
 *
 * 「编辑」现在承担两件事：**改文件名**（所有资产都有这个入口 —— 表格操作列那个
 * 独立的「改名」已经并进来了），以及在服务端说 `editable` 时改正文。
 *
 * 正文必须**先读回来**再让教师改：对话框里空着就是空文件，而"打开一份空表单、
 * 改两笔、保存"会把一份好好的文件覆盖成半截。不是文本的文件（zip、pdf…）
 * 连 GET 都不发 —— 服务端一定会 400，那是白跑一趟。
 */
async function openEditTextDialog(asset: AssetOut): Promise<void> {
  const contestId = contest.currentId
  if (!contestId) return
  textMode.value = 'edit'
  textTarget.value = asset
  textForm.filename = asset.filename
  textForm.content = ''
  textEditable.value = asset.editable
  // 用途不在这个入口里：换用途等于换它落到机器上的目标目录，那是另一个动作。
  // 这里跟着资产把它带进对话框只是为了让下拉显示的不是一个假值。
  uploadKind.value = asset.kind
  textDialog.value = true
  if (!asset.editable) return
  // 从这一刻起确认按钮就该是按不动的 —— 正文还没到，点下去存的是一份空文件
  textLoading.value = true
  try {
    const text = await assetApi.getText(contestId, asset.id)
    textForm.content = text.content
  } catch (error) {
    ElMessage.error((error as Error).message)
    // 把对话框收掉，避免它停在一份空表单上，让人以为"这个文件就是空的"
    textDialog.value = false
    textTarget.value = null
  } finally {
    textLoading.value = false
  }
}

/**
 * 编辑模式下有没有实质改动。
 *
 * 不能改正文的文件只提供改名，所以"名字没变"时确认按钮就该是灰的 —— 否则教师
 * 点了一次保存，拿到的是"文件名没有变化"这种他无法理解的报错。
 */
const textDirty = computed(() => {
  const asset = textTarget.value
  if (textMode.value !== 'edit' || !asset) return true
  if (textForm.filename.trim() !== asset.filename) return true
  return textEditable.value
})

/**
 * 保存一条已有的资产：同一个对话框、同一个提交按钮，分叉在"要不要写正文"那一步。
 *
 * 「改名」与「改正文」是服务端两个入口（`PATCH /assets/{id}` 与 `PUT .../text`），
 * 所以这里按需依次调用：名字变了先改名（它是换标签，失败率最低、也应该最先做完），
 * 然后才是正文。顺序反过来会出现"正文已经存成新的、改名却因为重名被拒"这种
 * 半截状态。
 *
 * 不能改正文的资产（zip、pdf…）只走改名那一步。
 */
const saveText = useMutation(
  async () => {
    const contestId = contest.currentId
    if (!contestId) throw new Error('未选择场次')
    const asset = textTarget.value
    if (!asset) throw new Error('没有选中文件')
    const name = textForm.filename.trim()
    if (!name) throw new Error('文件名不能为空')
    // 服务端会按路径段的规则校验，这里先挡一次明显的错，省一个来回
    if (name.includes('/') || name.includes('\\')) {
      throw new Error('文件名不能包含斜杠')
    }

    let renamedAsset: AssetOut | null = null
    if (name !== asset.filename) {
      renamedAsset = await assetApi.rename(asset.id, { filename: name })
    }

    if (!textEditable.value) {
      if (!renamedAsset) throw new Error('文件名没有变化')
      return { asset: renamedAsset, requeued: 0, renamed: true, contentSaved: false }
    }

    // 服务端的上限是**字节**（1 MB），不是字符数：中文一个字三字节，
    // 用 content.length 拦等于把上限放成三倍，于是点了保存才被拒。
    // 这个数与服务端的 `_TEXT_MAX_BYTES` 对齐 —— 它比「新建文本文件」的
    // 200000 字符上限宽，保证在界面上新建出来的正文都还能改。
    if (new TextEncoder().encode(textForm.content).length > 1024 * 1024) {
      throw new Error('正文超过 1 MB')
    }
    const saved = await assetApi.saveText(contestId, asset.id, textForm.content)
    return {
      asset: saved.asset,
      requeued: saved.requeued,
      renamed: renamedAsset !== null,
      contentSaved: true,
    }
  },
  {
    success: ({ asset, requeued, renamed, contentSaved }) => {
      // 只是改了名：这句要写清"已经落地的那份不受影响"，否则教师会以为改名能
      // 修正发出去的文件
      if (!contentSaved) {
        return `已改名为「${asset.filename}」`
      }
      const verb = renamed ? '已改名并保存' : '已保存'
      // 只有真重排了才提台数：硬报"重新排队给 0 台机器"会让教师以为出错了
      return requeued > 0
        ? `${verb}「${asset.filename}」，重新下发 ${requeued} 台`
        : `${verb}「${asset.filename}」`
    },
    onDone: async () => {
      textDialog.value = false
      textTarget.value = null
      await assets.reload()
    },
  },
)

const createText = useMutation(
  async () => {
    // 编辑走 saveText 那条 PUT。这里挡一道不是多疑：两条路共用一个对话框，
    // 少这一步的话，将来有人忘了给编辑模式加上分支，就会静默地**新建**一条
    // 同名资产（而不是改掉原来那条），界面上看起来一切正常。
    if (textMode.value === 'edit') throw new Error('这是一条已有的文件，请走保存')
    const contestId = contest.currentId
    if (!contestId) throw new Error('未选择场次')
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
      if (reused) return `已存在同名同内容的「${asset.filename}」`
      // 列表没看全时不敢断言"新建"：命中更早那一页上的旧资产同样是合法的。
      // 两种情况的最终状态完全一样（不会造出两份），差的只是收据的措辞。
      const verb = truncated ? '已保存' : '已新建'
      return `${verb}「${asset.filename}」（${formatBytes(asset.size)}）`
    },
    onDone: async () => {
      textDialog.value = false
      await assets.reload()
    },
  },
)

// --------------------------------------------------------------------------- //
// 资产：zip 密码 / 打包成 zip（打密码 / 改密码 / 生成随机密码）
//
// 用的是 InfoZIP 传统加密（ZipCrypto），因为学生机器上的 Archive Manager 只认
// 这一种：AES-256 的 zip 它打不开（除非另外装了 p7zip），传统加密的会弹框要密码。
//
// **它是弱加密**：已知明文攻击可以破，题面/样例这种已知结构的数据更是如此。
// 所以这里（以及对话框里）的定位是"挡得住随手翻看，挡不住有心人"，不许出现
// "安全加密"这类说法 —— 那会给教师一种它并不提供的保证。
//
// 非 zip 的资产也走同一个动作：服务端先把它包成 zip（成员名 = 原文件名，资产
// 文件名换成 <原基名>.zip），再套密码。是打包还是改密码由服务端按**内容**判定，
// 回执里的 `packaged` 说清是哪一种。
//
// 密码不存在服务端：它只活在同步生成的 password.txt 里。回执里那句"请自己抄
// 下来"不是客套 —— 对话框关掉之后，能找回它的地方就只有那份文件。
// --------------------------------------------------------------------------- //

const zipDialog = ref(false)
const zipTarget = ref<AssetOut | null>(null)
const zipStatus = ref<AssetZipPasswordOut | null>(null)
const zipLoading = ref(false)
/** 这次动作是「把非 zip 打包成 zip」还是「给已有的 zip 改密码」。 */
const zipPackMode = ref(false)
const zipForm = reactive({ password: '', oldPassword: '', generate: false })

/**
 * 行上那个动作按钮的字。
 *
 * 只看扩展名：列表一次 500 行，为每一行去探一次内容是不现实的。真正的判据在
 * 服务端 —— 点开之后 GET 会按内容把对话框切成打包 / 改密码（见下面）。
 */
function zipActionLabel(name: string): string {
  return name.toLowerCase().endsWith('.zip') ? '密码' : '打包成 zip'
}

/** 服务端生成的密码文件（`api/admin.py` 里的 `PASSWORD_FILENAME`）。 */
const PASSWORD_FILENAME = 'password.txt'

/**
 * 这一行给不给打包 / 改密码入口。
 *
 * `password.txt` 排除在外：它是服务端生成的**单件**（正文由
 * `_upsert_password_asset` 维护），不是教学资产。把它打成 `password.zip` 会顺手
 * 再生成一份新的 `password.txt` —— 这个文件能自己滚下去，所以它连入口都不该有。
 */
function canZipAction(row: AssetOut): boolean {
  return row.filename.toLowerCase() !== PASSWORD_FILENAME
}

/** 打包后资产的新文件名。**只用于对话框里的预览**，真正改名的是服务端。 */
const zipNewName = computed(() => {
  const name = zipTarget.value?.filename ?? ''
  // 与服务端 `_packaged_filename` 同一条规则：只换最后一个后缀
  const stem = name.replace(/\.[^./\\]*$/, '')
  return `${stem || name}.zip`
})

function toggleGeneratePassword(): void {
  zipForm.generate = !zipForm.generate
  // 切到"生成"就把手填的清掉，避免提交时看不出到底用了哪一个
  if (zipForm.generate) zipForm.password = ''
}

/**
 * 打开对话框前先问一次状态。
 *
 * 这一次 GET 决定两件事：对话框里要不要出现"旧密码"，以及这次动作是打包还是
 * 改密码。判据是**内容**：一个叫 `.pdf` 的文件可能真是一个包，一个叫 `.zip` 的
 * 文件也可能根本不是。
 *
 * GET 对非 zip 回 400 `asset_not_zip` —— 那不是失败，而是"这次该打包"的信号：
 * POST 对非 zip 是打包成 zip，不是拒绝。其他错误才是真的错误。
 */
async function openZipPasswordDialog(asset: AssetOut): Promise<void> {
  const contestId = contest.currentId
  if (!contestId) return
  zipTarget.value = asset
  zipStatus.value = null
  // 先按扩展名给一个默认值，GET 回来之后以内容为准
  zipPackMode.value = !asset.filename.toLowerCase().endsWith('.zip')
  zipForm.password = ''
  zipForm.oldPassword = ''
  zipForm.generate = false
  zipLoading.value = true
  try {
    zipStatus.value = await assetApi.getZipPassword(contestId, asset.id)
    zipPackMode.value = false
    zipDialog.value = true
  } catch (error) {
    if (error instanceof ApiError && error.code === 'asset_not_zip') {
      // 不是 zip：这次动作是打包。它没有旧密码这回事，所以给一份本地状态
      zipStatus.value = { encrypted: false, filename: asset.filename }
      zipPackMode.value = true
      zipDialog.value = true
      return
    }
    ElMessage.error((error as Error).message)
    zipTarget.value = null
  } finally {
    zipLoading.value = false
  }
}

/** 提交按钮能不能点：已加密必须给旧密码，新密码要么填了要么让服务端生成。 */
const zipCanSubmit = computed(() => {
  if (zipLoading.value || !zipStatus.value) return false
  if (zipStatus.value.encrypted && !zipForm.oldPassword) return false
  return zipForm.generate || zipForm.password.length > 0
})

/**
 * 提交：服务端就地改同一个 asset id 的字节（非 zip 时先打包），并同步写一份
 * password.txt。
 *
 * 回执要写清三件事（**包重排了多少台 / 新密码是什么 / 还要去下发
 * password.txt**）—— 少了哪一件，教师都会在现场卡住。打包还要多说一句"文件名
 * 变了"：不然他会以为文件自己改了名字。
 */
const saveZipPassword = useMutation(
  async () => {
    const contestId = contest.currentId
    if (!contestId) throw new Error('未选择场次')
    const asset = zipTarget.value
    const status = zipStatus.value
    if (!asset || !status) throw new Error('没有选中文件')
    if (!zipForm.generate && !zipForm.password) {
      throw new Error('请填写新密码或生成随机密码')
    }
    if (status.encrypted && !zipForm.oldPassword) {
      throw new Error('请填写旧密码')
    }
    const saved = await assetApi.setZipPassword(contestId, asset.id, {
      password: zipForm.generate ? undefined : zipForm.password,
      generate: zipForm.generate,
      old_password: status.encrypted ? zipForm.oldPassword : undefined,
    })
    // 带上原名：回执里那句"改名"要能说清从哪个名字改过来的，而 zipTarget
    // 在 onDone 里会被清空
    return { saved, before: asset.filename }
  },
  {
    success: ({ saved, before }) => {
      const head =
        saved.packaged && saved.asset.filename !== before
          ? `已打包「${before}」为「${saved.asset.filename}」`
          : `已重新打包「${saved.asset.filename}」`
      return (
        `${head}，重新下发 ${saved.requeued} 台；` +
        `新密码：${saved.password}；` +
        `「${saved.password_asset.filename}」需另行下发`
      )
    },
    onDone: async () => {
      zipDialog.value = false
      zipTarget.value = null
      zipStatus.value = null
      await assets.reload()
    },
  },
)

// --------------------------------------------------------------------------- //
// 资产：删除
// --------------------------------------------------------------------------- //

/**
 * 删除资产用普通确认而不是打名字 —— 约定里的"打一遍名字"是给结构性数据用的。
 * 注意它在服务端也是**硬删**，没有墓碑（ADR-0003 的 2026-11 修订）。
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
      '引用它的下发任务与逐选手进度将一并删除，不可恢复；已落到机器上的文件不会撤回。',
      `删除「${asset.filename}」？`,
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
 * 它们就该直接躺在桌面上，选手双击解压就能看。
 *
 * 要按题分发时，教师自己把 `{player_no}/<题目名>` 填进来 —— 服务端**只认
 * `{player_no}` 这一个占位符**，别的 `{...}` 会被明确拒绝，不会偷偷变成一个
 * 目录名。界面不替他拼：拼错了（题目名改过、目录想换个层级）比手打更难发现。
 *
 * agent 侧的 `deploy_root` 默认就是桌面，所以这里的相对路径从桌面往下写。
 */
const form = reactive({
  assetId: undefined as number | undefined,
  targetKind: 'all' as 'all' | 'player' | 'group',
  playerIds: [] as number[],
  targetGroup: '',
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
  return `桌面/${form.destDir.trim().replace(/\{player_no\}/g, probe)}`.replace(/\/$/, '')
})

function openCreate(asset?: AssetOut): void {
  form.assetId = asset?.id
  form.targetKind = 'all'
  form.playerIds = []
  form.targetGroup = groups.value[0] ?? ''
  // 默认落到**桌面根目录** —— 题面 zip、样例 zip、须知这类东西就该直接摆在
  // 桌面上。要按题分发附件时，教师自己填 `{player_no}/<题目名>`。
  form.destDir = ''
  form.mode = 'overwrite'
  createDialog.value = true
}

const createDeploy = useMutation(
  async () => {
    const contestId = contest.currentId
    if (!contestId) throw new Error('未选择场次')
    if (!form.assetId) throw new Error('请选择要下发的文件')
    if (form.targetKind === 'player' && form.playerIds.length === 0) {
      throw new Error('请选择选手')
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
    success: (task) => `已创建下发，目标 ${task.total} 台`,
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
      '未完成的目标将停止下发；已收到文件的机器不回滚。',
      `取消下发「${task.filename}」？`,
      { type: 'warning', confirmButtonText: '取消下发', cancelButtonText: '返回' },
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
      '将删除该下发记录及逐选手进度，不可恢复；未完成的机器不再收到此文件，已落地的文件不会撤回。',
      `删除下发记录「${task.filename}」？`,
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

// --------------------------------------------------------------------------- //
// 快捷操作入口：?do=new 打开新建下发
// --------------------------------------------------------------------------- //

watch(
  () => route.query.do,
  (action) => {
    if (action !== 'new') return
    openCreate()
    void router.replace({ query: { ...route.query, do: undefined } })
  },
  { immediate: true },
)
</script>

<template>
  <PageShell title="文件下发" :error="listError" retryable @retry="refreshAll">
    <template #toolbar>
      <el-button :loading="assets.loading.value || tasks.loading.value" @click="refreshAll">
        <el-icon><Refresh /></el-icon>刷新
      </el-button>
      <el-button type="primary" :disabled="!assets.total.value" @click="openCreate()">
        <el-icon><Promotion /></el-icon>新建下发
      </el-button>
    </template>

    <input ref="fileInput" type="file" style="display: none" @change="handleFileChange" />

    <SectionCard title="可下发文件" :meta="`${assets.total.value} 个`" flush>
      <template #extra>
        <span class="field-label">类别</span>
        <el-select v-model="uploadKind" style="width: 100px">
          <el-option v-for="kind in KIND_PRESETS" :key="kind" :label="kind" :value="kind" />
        </el-select>
        <el-checkbox v-model="uploadPackage">打包成 zip</el-checkbox>
        <el-input
          v-if="uploadPackage"
          v-model="uploadZipPassword"
          style="width: 170px"
          placeholder="密码，留空自动生成"
        />
        <el-button type="primary" plain :loading="upload.pending.value" @click="pickFile">
          <el-icon><Upload /></el-icon>上传文件
        </el-button>
        <el-dropdown trigger="click">
          <el-button>
            新建文本<el-icon class="el-icon--right"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item @click="openTextDialog('create')">新建文本文件</el-dropdown-item>
              <el-dropdown-item @click="openTextDialog('notice', NOTICE_FILENAME)">
                写考场公告
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </template>

      <DataTable
        :rows="assets.rows.value"
        :row-key="(row: AssetOut) => row.id"
        :loading="assets.loading.value"
        :total="assets.total.value"
        :page="assets.page.value"
        :page-size="assets.pageSize.value"
        empty-text="无可下发文件"
        @update:page="assets.setPage"
        @update:pageSize="assets.setPageSize"
      >
        <template #columns>
          <el-table-column label="文件名" min-width="200">
            <template #default="{ row }">
              <span class="mono">{{ row.filename }}</span>
              <el-tag
                v-if="row.filename.toLowerCase().endsWith('.zip')"
                effect="plain"
                class="name-tag"
              >
                压缩包
              </el-tag>
            </template>
          </el-table-column>

          <el-table-column label="类别" width="110">
            <template #default="{ row }">
              <span class="cell-sub">{{ kindLabel(row.kind) }}</span>
            </template>
          </el-table-column>

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

          <el-table-column label="上传时间" width="170">
            <template #default="{ row }">
              <span class="cell-sub">{{ formatTime(row.created_at) }}</span>
            </template>
          </el-table-column>

          <!-- 编辑 = 改名 + （editable 时）改正文 -->
          <el-table-column label="操作" width="260" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openCreate(row)">下发</el-button>
              <el-button link type="primary" size="small" @click="openEditTextDialog(row)">
                编辑
              </el-button>
              <el-button
                v-if="canZipAction(row)"
                link
                type="primary"
                size="small"
                :loading="zipLoading && zipTarget?.id === row.id"
                @click="openZipPasswordDialog(row)"
              >
                {{ zipActionLabel(row.filename) }}
              </el-button>
              <el-button link type="danger" size="small" @click="askRemoveAsset(row)">删除</el-button>
            </template>
          </el-table-column>
        </template>

        <template #empty>
          <div>无可下发文件</div>
          <el-button link type="primary" style="margin-top: 8px" @click="pickFile">上传文件</el-button>
        </template>
      </DataTable>
    </SectionCard>

    <SectionCard title="下发任务" :meta="`${tasks.total.value} 条`" flush>
      <DataTable
        :rows="tasks.rows.value"
        :row-key="(row: DeployTaskOut) => row.id"
        :loading="tasks.loading.value"
        :total="tasks.total.value"
        :page="tasks.page.value"
        :page-size="tasks.pageSize.value"
        empty-text="无下发任务"
        @update:page="tasks.setPage"
        @update:pageSize="tasks.setPageSize"
      >
        <template #columns>
          <!-- 逐选手进度随 include_targets=true 一起带回 -->
          <el-table-column type="expand">
            <template #default="{ row }">
              <div class="expand-body">
                <el-table v-if="row.targets?.length" :data="row.targets">
                  <el-table-column label="考号" prop="player_no" width="110" />
                  <el-table-column label="状态" width="100">
                    <template #default="scope">
                      <el-tag :type="statusType(scope.row.status)" effect="light">
                        {{ statusLabel(scope.row.status) }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column label="进度" min-width="160">
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
                      <span v-if="scope.row.last_error" class="error-text cell-error">
                        {{ scope.row.last_error }}
                      </span>
                      <span v-else class="muted">—</span>
                    </template>
                  </el-table-column>
                </el-table>
                <div v-else class="muted">无目标</div>
              </div>
            </template>
          </el-table-column>

          <el-table-column label="文件" min-width="180">
            <template #default="{ row }">
              <span class="mono">{{ row.filename }}</span>
              <div class="cell-sub">{{ formatBytes(row.size) }}</div>
            </template>
          </el-table-column>

          <el-table-column label="目标" width="190">
            <template #default="{ row }">
              <el-tag effect="plain">{{ targetKindLabel(row.target_kind) }}</el-tag>
              <div class="cell-sub">目录：{{ row.dest_dir || '桌面' }}</div>
              <div class="cell-sub">同名文件：{{ row.mode === 'skip_exist' ? '跳过' : '覆盖' }}</div>
            </template>
          </el-table-column>

          <el-table-column label="进度" min-width="220">
            <template #default="{ row }">
              <el-progress
                :percentage="percent(row.done + row.failed, row.total)"
                :status="row.failed > 0 ? 'exception' : row.status === 'done' ? 'success' : undefined"
                :stroke-width="12"
              />
              <div class="cell-sub">
                完成 {{ row.done }} · 失败 {{ row.failed }} · 待处理 {{ row.pending }}<template
                  v-if="row.cancelled"
                > · 已取消 {{ row.cancelled }}</template> / 共 {{ row.total }} 台
              </div>
            </template>
          </el-table-column>

          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag :type="statusType(row.status)" effect="light">
                {{ statusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>

          <el-table-column label="创建时间" width="170">
            <template #default="{ row }">
              <span class="cell-sub">{{ formatTime(row.created_at) }}</span>
            </template>
          </el-table-column>

          <el-table-column label="操作" width="160" fixed="right">
            <template #default="{ row }">
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
              <el-button
                v-if="row.status !== 'done' && row.status !== 'cancelled'"
                link
                type="warning"
                size="small"
                :loading="cancelTask.pending.value"
                @click="askCancel(row)"
              >
                取消
              </el-button>
              <el-button link type="danger" size="small" @click="askRemoveTask(row)">删除</el-button>
            </template>
          </el-table-column>
        </template>

        <template #empty>
          <div>无下发任务</div>
          <el-button
            link
            type="primary"
            style="margin-top: 8px"
            :disabled="!assets.total.value"
            @click="openCreate()"
          >
            新建下发
          </el-button>
        </template>
      </DataTable>
    </SectionCard>

    <FormDialog
      v-model="createDialog"
      title="新建下发"
      :submitting="createDeploy.pending.value"
      confirm-text="下发"
      @submit="createDeploy.run(undefined)"
    >
      <el-form label-width="80px">
        <el-form-item label="文件">
          <el-select v-model="form.assetId" placeholder="选择文件" filterable style="width: 100%">
            <el-option
              v-for="asset in assets.rows.value"
              :key="asset.id"
              :label="`${asset.filename}（${formatBytes(asset.size)}）`"
              :value="asset.id"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="范围">
          <el-radio-group v-model="form.targetKind">
            <el-radio-button value="all">全员</el-radio-button>
            <el-radio-button value="group">按分组</el-radio-button>
            <el-radio-button value="player">指定选手</el-radio-button>
          </el-radio-group>
        </el-form-item>

        <el-form-item v-if="form.targetKind === 'group'" label="分组">
          <el-select
            v-model="form.targetGroup"
            :placeholder="groups.length ? '选择分组' : '无分组'"
            :disabled="!groups.length"
            style="width: 100%"
          >
            <el-option v-for="group in groups" :key="group" :label="group" :value="group" />
          </el-select>
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

        <el-form-item label="目标目录">
          <el-input v-model="form.destDir" placeholder="例如 {player_no}/A，留空为桌面" />
          <div class="cell-sub">落点：<code class="mono">{{ destPreview }}</code></div>
        </el-form-item>

        <el-form-item label="同名文件">
          <el-radio-group v-model="form.mode">
            <el-radio value="overwrite">覆盖</el-radio>
            <el-radio value="skip_exist">跳过</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
    </FormDialog>

    <!-- 新建文本 / 写公告 / 编辑已有文件共用 -->
    <FormDialog
      v-model="textDialog"
      :title="textTitle"
      width="640px"
      :submitting="createText.pending.value || saveText.pending.value"
      :disabled="textLoading || !textForm.filename.trim() || (textMode === 'edit' && !textDirty)"
      :confirm-text="textMode === 'edit' ? '保存' : '创建'"
      @submit="textMode === 'edit' ? saveText.run(undefined) : createText.run(undefined)"
    >
      <el-alert
        v-if="textMode === 'edit'"
        type="warning"
        :closable="false"
        show-icon
        style="margin-bottom: 14px"
      >
        <template #title>已落到机器上的文件不随改名变化</template>
        <template v-if="textEditable" #default>改正文后，已完成的机器将重新下发。</template>
      </el-alert>

      <el-form v-loading="textLoading" label-width="80px">
        <el-form-item label="文件名" required>
          <el-input v-model="textForm.filename" placeholder="例如 须知.txt" maxlength="255" />
        </el-form-item>

        <el-form-item label="类别">
          <el-select v-model="uploadKind" :disabled="textMode === 'edit'" style="width: 100%">
            <el-option v-for="kind in KIND_PRESETS" :key="kind" :label="kind" :value="kind" />
          </el-select>
        </el-form-item>

        <el-form-item v-if="textMode !== 'edit' || textEditable" label="内容">
          <el-input
            v-model="textForm.content"
            class="text-content"
            type="textarea"
            :rows="14"
            maxlength="200000"
            placeholder="例如 比赛结束后不要关机"
          />
        </el-form-item>
        <el-form-item v-else label="内容">
          <span class="muted">仅可改名</span>
        </el-form-item>
      </el-form>
    </FormDialog>

    <!-- zip 加密码 / 改密码 / 打包成 zip -->
    <FormDialog
      v-model="zipDialog"
      :title="zipPackMode ? '打包成 zip' : '设置 zip 密码'"
      :submitting="saveZipPassword.pending.value"
      :disabled="!zipCanSubmit"
      :confirm-text="zipPackMode ? '打包' : '重新打包'"
      @submit="saveZipPassword.run(undefined)"
    >
      <el-form label-width="80px">
        <el-form-item label="文件">
          <span class="mono">{{ zipTarget?.filename }}</span>
          <template v-if="zipPackMode">
            <el-icon class="arrow"><Right /></el-icon>
            <span class="mono">{{ zipNewName }}</span>
          </template>
        </el-form-item>

        <el-form-item v-if="zipStatus && !zipPackMode" label="当前">
          <el-tag v-if="zipStatus.encrypted" type="warning" effect="light">有密码</el-tag>
          <el-tag v-else type="info" effect="plain">无密码</el-tag>
        </el-form-item>

        <el-form-item label="加密方式">
          <span>ZipCrypto</span>
          <el-tag type="warning" effect="plain" class="name-tag">弱加密</el-tag>
        </el-form-item>

        <el-form-item v-if="zipStatus?.encrypted" label="旧密码">
          <el-input
            v-model="zipForm.oldPassword"
            type="password"
            show-password
            placeholder="见旧的 password.txt"
          />
        </el-form-item>

        <el-form-item label="新密码">
          <div class="password-row">
            <el-input
              v-model="zipForm.password"
              :disabled="zipForm.generate"
              :placeholder="zipForm.generate ? '提交后生成' : '例如 noip2026'"
            />
            <el-button @click="toggleGeneratePassword">
              {{ zipForm.generate ? '手动填写' : '生成随机密码' }}
            </el-button>
          </div>
        </el-form-item>
      </el-form>

      <div class="cell-sub">
        将同时更新 <code class="mono">password.txt</code>，需另行下发；已完成的机器将重新下发此包。
      </div>
    </FormDialog>
  </PageShell>
</template>

<style scoped>
.field-label {
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.name-tag {
  margin-left: 6px;
}

.expand-body {
  padding: 8px 20px 12px 48px;
}

.cell-error {
  font-size: 12px;
}

.arrow {
  margin: 0 6px;
  color: var(--sj-text-3);
}

.password-row {
  display: flex;
  gap: 8px;
  width: 100%;
}

.text-content :deep(textarea) {
  font-family: var(--sj-mono);
}
</style>

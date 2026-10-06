<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { releaseApi, bootstrapKeyApi } from '@/api'
import type {
  BootstrapKeyOut,
  ReleaseOut,
  ReleaseSourceOut,
  UpgradeStatusOut,
} from '@/api/types'
import { useMutation } from '@/composables/useMutation'
import { usePolling } from '@/composables/usePolling'
import { formatBytes, formatTime } from '@/utils/format'

/**
 * 发布状态。
 *
 * 它**不是一个分页列表**：`GET /releases` 返回的是 `UpgradeStatusOut` 一个对象，
 * 版本历史是它的一个字段。所以这一页不用 `useList`，但改数据的动作仍然走
 * `useMutation` —— 按钮的 loading 与错误提示不需要各写一遍。
 */
const status = ref<UpgradeStatusOut | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)

const fileInput = ref<HTMLInputElement>()
const version = ref('')
const notes = ref('')

const activeRelease = computed(() => status.value?.active_release ?? null)
const releases = computed<ReleaseOut[]>(() => status.value?.releases ?? [])
const rolledOutCount = computed(() => releases.value.filter((r) => r.rolled_out).length)
const yankedCount = computed(() => releases.value.filter((r) => r.yanked).length)

async function refresh(): Promise<void> {
  loading.value = true
  try {
    status.value = await releaseApi.status()
    error.value = null
  } catch (err) {
    error.value = (err as Error).message
  } finally {
    loading.value = false
  }
}

// 发布状态变化很慢，30 秒足够
usePolling(refresh, { interval: 30000 })

/**
 * 能否签发升级包。
 *
 * 这是一个**服务端配置状态**，不是这一页的问题：没有私钥就签不出包，
 * 而没有签名的包 Agent 一律拒绝。所以它一旦为 false，整页的发布功能都是死的，
 * 必须让教师一眼看到"这不是我没点对按钮"，以及要在哪里配。
 */
const signingReady = computed(() => status.value?.signing_available ?? false)
/** 状态还没回来时不下"已配置"的结论 —— 否则首屏会闪一下"没有私钥"的红条。 */
const signingKnown = computed(() => status.value !== null)

function pickFile(): void {
  fileInput.value?.click()
}

/**
 * 「发布当前版本」：本机有没有可构建的源码。
 *
 * 取不到就当没有 —— 这是这一页的**次要功能**，它挂了不该把"上传/铺开"也标红。
 * 服务端正则永远回 200（没有源码是正常状态），所以这里几乎不会真的失败。
 */
const source = ref<ReleaseSourceOut | null>(null)
const buildVersion = ref('')
const buildNotes = ref('')
/**
 * 要不要把「统一注册密钥」一起塞进这个包。
 *
 * **默认关**：装机入口（`/api/v1/agent/install/*`）刻意不鉴权（空机器上没有任何
 * 凭据可用），所以附带密钥的包，任何能打开装机页的人都能下载到 —— 等于一张
 * 能注册进这台服务端的通行证。警示文案写死在模板里，别抄成"建议"。
 */
const includeBootstrapKey = ref(false)

async function loadSource(): Promise<void> {
  try {
    source.value = await releaseApi.source()
    if (!buildVersion.value) buildVersion.value = source.value.version ?? ''
  } catch {
    source.value = null
  }
}

onMounted(loadSource)

const buildable = computed(() => source.value?.available === true)

/**
 * 提交的版本号和源码里那个不一致。
 *
 * 服务端也会拒（409 `version_mismatch`），这里提前说是因为它的成因只有两种，
 * 两种都很具体：要么改代码忘了改 `__version__`，要么手输时打错了。
 */
const versionDrifts = computed(
  () => buildable.value && !!buildVersion.value.trim() && buildVersion.value.trim() !== source.value?.version,
)

/** 同名版本是否已经在历史里。会覆盖它那份包（未铺开的话）。 */
const versionExists = computed(() =>
  releases.value.some((row) => row.version === buildVersion.value.trim()),
)

/**
 * 本机没有要内嵌进包里的公钥。
 *
 * 这时**服务端会拒绝构建**（能签、但机器验不了 —— 铺开之后所有机器一动不动，
 * 而界面上一切正常）。所以这里不摆一个点了必然报错的按钮，直接把缺哪个文件
 * 写出来。
 */
const trustAnchorMissing = computed(
  () => buildable.value && source.value?.public_key == null,
)

const canBuild = computed(
  () => buildable.value && !trustAnchorMissing.value && !!buildVersion.value.trim(),
)

async function copyText(value: string | undefined, what: string): Promise<void> {
  if (!value) return
  try {
    await navigator.clipboard.writeText(value)
    ElMessage.success(`已复制${what}`)
  } catch {
    // 非 HTTPS 或浏览器策略下剪贴板不可用，提示手工复制而不是静默失败
    ElMessage.warning('浏览器不允许自动复制，请手工选中复制')
  }
}

/**
 * 给空机器的安装命令。
 *
 * 一台什么都没有的机器上只有 shell，所以起头那一下必须是 curl —— 而机器该去哪个
 * 地址拿，只有服务端自己知道（见 `/releases/source` 的 `public_url`）。
 *
 * **服务端报不出自己的地址时这条命令就不显示**：那样拼出来的会是 `127.0.0.1`，
 * 而在别的机器上它指向那台机器自己 —— 教师会照着敲，然后看着"连不上"去查网线与
 * 防火墙。宁可这里空着，也不能给一条必然是错的命令。
 */
const installCommand = computed(() => {
  const base = source.value?.public_url
  if (!activeRelease.value || !base) return ''
  return (
    `curl -fsSL ${base}/api/v1/agent/install/bootstrap.sh | ` +
    `sudo sh -s -- --server ${base}`
  )
})

const build = useMutation(
  () =>
    releaseApi.build(
      buildVersion.value.trim(),
      buildNotes.value.trim(),
      'stable',
      includeBootstrapKey.value,
    ),
  {
    success: (release) =>
      release.bootstrap_key_id
        ? `已构建并签发 ${release.version}，包内附带统一注册密钥 ${release.bootstrap_key_label}（未铺开，Agent 还看不到）`
        : `已构建并签发 ${release.version}（未铺开，Agent 还看不到）`,
    onDone: async (release) => {
      buildNotes.value = ''
      includeBootstrapKey.value = false
      await refresh()
      // 回执里的那一句"等于通行证"不能只写在页面上：发完版的人多半已经滚走了。
      // 但服务端**不返回明文**（库里只有哈希），能说的就是"哪一把、什么后果"。
      if (release.bootstrap_key_id) {
        ElMessage.warning({
          message:
            `这个包附带了一把统一注册密钥（${release.bootstrap_key_label}）—— 它等于一张「能注册进这台服务端」的通行证，` +
            '任何能打开装机页的人都能下载它。发完就吊销这一把，或者换一个不带密钥的版本铺开。',
          duration: 10000,
          showClose: true,
        })
      }
    },
  },
)

const upload = useMutation(
  (file: File) => releaseApi.upload(file, version.value.trim(), notes.value.trim()),
  {
    // 回执用服务端算出来的版本号：服务端在覆盖同名版本时会走另一条分支，
    // 界面自己拼一句"已上传 xxx"会把这件事盖掉
    success: (release) => `已上传版本 ${release.version}（尚未铺开，Agent 还看不到）`,
    onDone: async () => {
      version.value = ''
      notes.value = ''
      await refresh()
    },
  },
)

function handleFileChange(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  // 立刻清空 value：否则连续选同一个文件不会再触发 change（"传第二遍没反应"）
  input.value = ''
  if (!file) return
  // 版本号是签名之外的另一个判据，空着传上去之后只剩 sha256 可认，先拦住
  if (!version.value.trim()) {
    ElMessage.warning('请先填写版本号，再选择安装包')
    return
  }
  void upload.run(file)
}

// 两个动作都返回更新后的 ReleaseOut，所以回执里带上服务端算出来的版本号 ——
// 那是"这一下动的是哪个版本"的唯一权威答案
const rolloutMutation = useMutation((release: ReleaseOut) => releaseApi.rollout(release.id), {
  success: (result: ReleaseOut) => `已开始铺开 ${result.version}，Agent 下一轮检查就会拿到它`,
  onDone: () => refresh(),
})

const yankMutation = useMutation((release: ReleaseOut) => releaseApi.yank(release.id), {
  success: (result: ReleaseOut) => `已撤回 ${result.version}，Agent 不会再被提供它`,
  onDone: () => refresh(),
})

/**
 * 铺开 = 开始向 Agent 提供这个版本。
 *
 * 服务端同一时刻只允许一个版本处于铺开状态，铺开新版本时会**自动撤回**旧的，
 * 所以确认文案里要把当前那个版本的名字写出来 —— 否则"我点了新版本，
 * 旧版本怎么没了"会变成一次惊吓。
 */
async function rollout(release: ReleaseOut): Promise<void> {
  const previous = activeRelease.value
  try {
    await ElMessageBox.confirm(
      `开始向所有考试机提供版本 ${release.version}？\n\n` +
        '铺开之后 Agent 就会去拉它 —— 但真正做什么取决于每台机器的 upgrade.mode：' +
        'off（默认）只记录「有新版本」、stage 下载并验签但不激活、apply 才会切换并重启。\n\n' +
        (previous
          ? `同一时刻只能有一个版本在铺开，所以 ${previous.version} 会被自动下架。`
          : '当前没有别的版本在铺开。'),
      '铺开版本',
      { type: 'warning', confirmButtonText: '开始铺开', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await rolloutMutation.run(release)
}

/** 撤回 = 不再向 Agent 提供它。已经升上去的机器不会因此降级。 */
async function yank(release: ReleaseOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `撤回版本 ${release.version}？\n\n` +
        '撤回之后 Agent 不会再被提供这个版本（包括还在排队下载的）。\n' +
        '但它**不能把已经升级上去的机器降回去** —— 那要靠客户端侧的回滚（升级后连续启动失败会退回上一版本）。',
      '撤回版本',
      { type: 'warning', confirmButtonText: '撤回', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await yankMutation.run(release)
}

/**
 * 删除发布记录。
 *
 * 这是硬删除，但约定里没有"输入名称确认"的要求，所以用一句写明后果的普通
 * 确认就够。真正要紧的是说清楚：删的是**记录**，包文件是按内容寻址存在
 * blob 存储里的，只有没有别的记录引用同一份内容时才会被顺手回收 ——
 * 也就是说这个按钮不会把已经装到机器上的东西弄坏，但会让"当时推的是哪个包"
 * 失去对照物。
 */
async function removeRelease(release: ReleaseOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `删除版本 ${release.version} 的发布记录？\n\n` +
        '包文件不会动，但删掉之后「当时推的是哪个包」就没有对照物了。',
      '删除发布记录',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await removeMutation.run(release)
}

const removeMutation = useMutation((release: ReleaseOut) => releaseApi.remove(release.id), {
  onDone: () => refresh(),
})

/**
 * 吊销这个版本附带的那把统一注册密钥。
 *
 * 复用现成的密钥吊销接口（`POST /bootstrap-keys/{id}/revoke`）—— 不另开一条"让版本
 * 去吊销自己的密钥"的捷径：密钥就是密钥，能吊销它的地方越少越好，而这条路已经
 * 被「机器配对」那一页和 CLI 用过了。
 *
 * **吊销不会删掉版本记录**（服务端那边 `bootstrap_key_id` 保持不变，只是那把密钥
 * 变成已吊销），所以事后还能回答"这个包当年带的是哪把钥匙"。
 */
const revokeKeyMutation = useMutation(
  (keyId: number) => bootstrapKeyApi.revoke(keyId),
  {
    success: (key: BootstrapKeyOut) =>
      `已吊销统一注册密钥 ${key.label || `#${key.id}`} —— 它不能再注册新机器，已注册的机器不受影响`,
    onDone: () => refresh(),
  },
)

async function revokeKey(release: ReleaseOut): Promise<void> {
  if (!release.bootstrap_key_id) return
  try {
    await ElMessageBox.confirm(
      `吊销版本 ${release.version} 附带的统一注册密钥（${release.bootstrap_key_label ?? `#${release.bootstrap_key_id}`}）？\n\n` +
        '吊销之后**再用这个包装机就注册不上了** —— 这通常正是想要的效果：' +
        '带密钥的包任何能打开装机页的人都下载得到，发完就该把这一把收掉。\n' +
        '已经用别的办法注册好的机器不受影响（它们手里是各自的凭据，不是这把密钥）。\n' +
        '版本记录会留着，随时能回看"当年发的是哪个包、带的是哪把钥匙"。',
      '吊销统一注册密钥',
      { type: 'warning', confirmButtonText: '吊销', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await revokeKeyMutation.run(release.bootstrap_key_id)
}

function statusTag(release: ReleaseOut): {
  text: string
  type: 'success' | 'info' | 'warning'
} {
  if (release.yanked) return { text: '已撤回', type: 'info' }
  if (release.rolled_out) return { text: '铺开中', type: 'success' }
  return { text: '未铺开', type: 'warning' }
}

// 编辑备注：上传时可能先空着，事后补上"这个版本修了什么"，
// 出问题时回看历史才有线索
const editVisible = ref(false)
const editForm = reactive({ id: 0, version: '', notes: '' })

function openEdit(release: ReleaseOut): void {
  editForm.id = release.id
  editForm.version = release.version
  editForm.notes = release.notes ?? ''
  editVisible.value = true
}

const editSave = useMutation(
  () => releaseApi.update(editForm.id, { notes: editForm.notes.trim() || null }),
  {
    success: (result: ReleaseOut) => `已保存 ${result.version} 的备注`,
    onDone: async () => {
      editVisible.value = false
      await refresh()
    },
  },
)
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h2 class="page-title">Agent 发布</h2>
      </div>
      <div class="toolbar">
        <el-button size="small" :loading="loading" @click="refresh">刷新</el-button>
      </div>
    </div>

    <el-alert
      v-if="error"
      type="error"
      :closable="false"
      show-icon
      :title="error"
      style="margin-bottom: 12px"
    >
      <template #default>
        <div class="error-actions">
          <el-button size="small" type="primary" plain @click="refresh">重试</el-button>
        </div>
      </template>
    </el-alert>

    <!--
      服务端没有配置发布签名私钥。
      这不是"这一页没配好"，而是整台服务端的能力缺失：签不出包，而没有签名的包
      Agent 一律拒绝，于是**任何一台机器都不可能被升级**。所以它用一个显眼的
      卡片而不是普通提示条，并把"在哪里配"一并写出来。
    -->
    <el-card
      v-if="signingKnown && !signingReady"
      shadow="never"
      class="section blocked-card"
    >
      <template #header>
        <span class="blocked-title">服务端没有配置发布签名私钥 —— 自更新功能整体关闭</span>
      </template>

      <el-descriptions :column="1" size="small" border style="margin-top: 12px">
        <el-descriptions-item label="第一步：生成密钥对">
          <span class="mono">
            python -m syncoj_server.cli genkey --out /etc/syncoj/release-key.pem
          </span>
        </el-descriptions-item>
        <el-descriptions-item label="第二步：服务端指向私钥">
          <span class="mono">SYNCOJ_RELEASE_KEY=/etc/syncoj/release-key.pem</span>
          ，然后重启服务端
        </el-descriptions-item>
        <el-descriptions-item label="第三步：分发公钥">
          把公钥放到每台考试机的
          <span class="mono">/etc/syncoj/release-key.pub.json</span>
          ，并在 agent.ini 的 <span class="mono">[upgrade]</span> 段写
          <span class="mono">public_key =</span> 指向它
        </el-descriptions-item>
        <el-descriptions-item v-if="status?.error" label="当前错误">
          <span class="mono">{{ status.error }}</span>
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <!--
      「发布当前版本」：服务端本机仓库里就有 agent/ 源码时，不必先手工打包再上传。
      放在「上传新版本」前面是因为它是**正常路径**；上传那条留给"服务端没有源码
      的部署"（生产上服务端常常只是一个 pip 装出来的实例）。
    -->
    <el-card v-if="signingReady && buildable" shadow="never" class="section">
      <template #header><span>发布当前版本</span></template>

      <div class="upload-row">
        <el-input v-model="buildVersion" placeholder="版本号" style="width: 160px" />
        <el-input v-model="buildNotes" placeholder="发布说明（可选）" style="flex: 1" />
        <el-button
          type="primary"
          :loading="build.pending.value"
          :disabled="!canBuild"
          @click="build.run(undefined)"
        >
          构建并签发
        </el-button>
      </div>

      <p v-if="versionDrifts" class="cell-sub warn-hint">
        版本号和源码里写的不一致，服务端会拒绝
      </p>
      <p v-else-if="versionExists" class="cell-sub warn-hint">
        历史里已有这个版本号
      </p>
      <p v-if="trustAnchorMissing" class="cell-sub warn-hint">
        本机缺发布公钥，构建会被拒
      </p>

      <!--
        「附带统一注册密钥」。
        文案是**说得清的后果**，不是"建议"式的软话：这个包一旦流出，等于一张
        "能注册进这台服务端"的通行证 —— 因为装机入口刻意不鉴权（空机器上没有任何
        凭据可用），任何能打开装机页的人都能把它下载走。
      -->
      <div class="key-option">
        <el-checkbox v-model="includeBootstrapKey" :disabled="!canBuild">
          附带统一注册密钥
        </el-checkbox>
      </div>
    </el-card>

    <el-card v-else-if="signingReady && source && !source.available" shadow="never" class="section">
      <template #header><span>发布当前版本</span></template>
      <p class="cell-sub" style="margin: 0">
        {{ source.reason }}
      </p>
    </el-card>

    <el-card v-if="signingReady" shadow="never" class="section">
      <template #header><span>上传新版本</span></template>
      <div class="upload-row">
        <el-input v-model="version" placeholder="版本号，例如 0.1.1" style="width: 160px" />
        <el-input v-model="notes" placeholder="发布说明（可选）" style="flex: 1" />
        <input ref="fileInput" type="file" style="display: none" @change="handleFileChange" />
        <el-button :loading="upload.pending.value" @click="pickFile">
          选择安装包并上传
        </el-button>
      </div>
      <p v-if="status?.key_id" class="cell-sub">
        当前用于签名的密钥：<span class="mono">{{ status.key_id }}</span>
      </p>
    </el-card>

    <el-card v-if="activeRelease" shadow="never" class="section">
      <template #header><span>当前铺开版本</span></template>
      <div class="active-box">
        <div>
          <div class="active-version">{{ activeRelease.version }}</div>
          <div class="cell-sub">
            {{ formatBytes(activeRelease.size) }} ·
            发布于 {{ formatTime(activeRelease.published_at) }}
          </div>
        </div>
        <el-button
          type="danger"
          plain
          size="small"
          :loading="yankMutation.pending.value"
          @click="yank(activeRelease)"
        >
          撤回
        </el-button>
      </div>
      <!--
        给空机器的安装命令。**只发已铺开的版本**（与升级同一个判据），所以它必须
        出现在这块"当前铺开版本"卡片里 —— 没铺开就没有可安装的东西。
        服务端报不出自己地址时这条不显示，原因见 installCommand 的注释。
      -->
      <template v-if="installCommand">
        <div class="install-row">
          <div class="install-label">在新机器上装 Agent：</div>
          <code class="install-command">{{ installCommand }}</code>
          <el-button size="small" @click="copyText(installCommand, '安装命令')">复制</el-button>
        </div>
      </template>
      <p v-else class="cell-sub warn-hint">
        报不出对外地址，给不出安装命令 —— 先配 SYNCOJ_PUBLIC_URL。
      </p>
    </el-card>

    <el-card v-else-if="signingReady && !loading" shadow="never" class="section">
      <template #header><span>当前没有铺开任何版本</span></template>
    </el-card>

    <el-card shadow="never">
      <template #header><span>版本历史（{{ releases.length }}）</span></template>
      <el-table :data="releases" v-loading="loading" size="small" border>
        <el-table-column label="版本" prop="version" width="120">
          <template #default="{ row }">
            <span class="mono">{{ row.version }}</span>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="statusTag(row).type" size="small" effect="plain">
              {{ statusTag(row).text }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="大小" width="100" align="right">
          <template #default="{ row }">{{ formatBytes(row.size) }}</template>
        </el-table-column>

        <el-table-column label="校验和" width="150">
          <template #default="{ row }">
            <el-tooltip :content="row.sha256">
              <span class="mono muted">{{ row.sha256.slice(0, 12) }}…</span>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="附带密钥" min-width="150">
          <template #default="{ row }">
            <span v-if="!row.bootstrap_key_id" class="muted">—</span>
            <el-tag
              v-else
              :type="row.bootstrap_key_revoked ? 'info' : 'danger'"
              size="small"
              effect="plain"
            >
              {{ row.bootstrap_key_label || `#${row.bootstrap_key_id}` }}
              {{ row.bootstrap_key_revoked ? '（已吊销）' : '（有效）' }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="说明" min-width="180">
          <template #default="{ row }">
            <span>{{ row.notes || '—' }}</span>
          </template>
        </el-table-column>

        <el-table-column label="上传时间" width="160">
          <template #default="{ row }">
            <span class="cell-sub">{{ formatTime(row.created_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="300" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="!row.rolled_out"
              link
              type="primary"
              size="small"
              :loading="rolloutMutation.pending.value"
              @click="rollout(row)"
            >
              铺开
            </el-button>
            <el-button
              v-else
              link
              type="danger"
              size="small"
              :loading="yankMutation.pending.value"
              @click="yank(row)"
            >
              撤回
            </el-button>
            <el-button link size="small" @click="openEdit(row)">备注</el-button>
            <!--
              就地吊销这一版附带的那把统一注册密钥。已经吊销过的（或这个包本来
              就没带密钥的）不显示 —— 留一个点了不会变的状态的按钮只会让人以为
              "再点一次才是真吊销"。
            -->
            <el-button
              v-if="row.bootstrap_key_id && !row.bootstrap_key_revoked"
              link
              type="danger"
              size="small"
              :loading="revokeKeyMutation.pending.value"
              @click="revokeKey(row)"
            >
              吊销密钥
            </el-button>
            <!--
              铺开中的版本服务端会直接拒绝删除（要先「撤回」再删），所以这里
              置灰并说明原因，而不是让教师点了再吃一句红字。
            -->
            <el-tooltip
              :disabled="!row.rolled_out"
              content="正在铺开。先「撤回」，再删除。"
            >
              <span>
                <el-button
                  link
                  type="danger"
                  size="small"
                  :disabled="row.rolled_out"
                  :loading="removeMutation.pending.value"
                  @click="removeRelease(row)"
                >
                  删除
                </el-button>
              </span>
            </el-tooltip>
          </template>
        </el-table-column>

        <template #empty>
          <div class="empty-block">还没有上传过任何 Agent 版本。</div>
        </template>
      </el-table>

      <p class="cell-sub">铺开中 {{ rolledOutCount }} 个，已撤回 {{ yankedCount }} 个。「删除」删的是发布记录，不是已经装到机器上的程序。</p>
    </el-card>

    <el-dialog v-model="editVisible" :title="`编辑版本 ${editForm.version} 的备注`" width="520px">
      <el-input
        v-model="editForm.notes"
        type="textarea"
        :rows="4"
        maxlength="2000"
        placeholder="例如：修复了扫描目录不存在时的崩溃"
      />
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="editSave.pending.value" @click="editSave.run(undefined)">
          保存
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.error-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.section {
  margin-bottom: 12px;
}

.upload-row {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}

.active-box {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  background: #f0f9eb;
  border: 1px solid #e1f3d8;
  border-radius: 4px;
}

.active-version {
  font-size: 22px;
  font-weight: 700;
  color: #67c23a;
  line-height: 1.2;
}

/* 没有私钥时用整块红卡，而不是一条会被划过去的提示条 */
.blocked-card :deep(.el-card__header) {
  background: #fef0f0;
  border-bottom: 1px solid #fde2e2;
}

.blocked-title {
  color: #f56c6c;
  font-weight: 700;
}

.blocked-lead {
  margin: 0 0 8px;
  line-height: 1.6;
}

.muted {
  color: #909399;
  font-size: 12px;
}

/* 提醒用的提示行：不是错误，是"这一步会怎样" */
.warn-hint {
  color: #e6a23c;
}

/*
  附带密钥那一块。文案是"后果"而不是"建议"，所以用偏红的警示色 + 左边框，
  让它和上面那些普通的 `.page-hint` 一眼分得开 —— 这一句读漏了，后果是这个包
  等于一张能注册进这台服务端的通行证。
*/
.key-option {
  margin-top: 12px;
}

.key-warning {
  margin: 8px 0 0;
  padding: 8px 12px;
  border-left: 3px solid #f56c6c;
  background: #fef0f0;
  color: #c45656;
  line-height: 1.6;
}

/*
  安装命令那一行：等宽、可换行、占满宽度。
  **不能截断** —— 一条被省略号吃掉尾巴的命令，教师复制过去就是语法错误，
  而报错会指向 shell，指不到"这里少了一段"。
*/
.install-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-top: 12px;
  flex-wrap: wrap;
}

.install-label {
  font-size: 13px;
  color: #606266;
  line-height: 26px;
}

.install-command {
  flex: 1;
  min-width: 280px;
  padding: 4px 8px;
  background: #f5f7fa;
  border: 1px solid #e4e7ed;
  border-radius: 4px;
  font-size: 12px;
  line-height: 18px;
  word-break: break-all;
  white-space: pre-wrap;
}
</style>

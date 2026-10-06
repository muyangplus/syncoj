<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { releaseApi } from '@/api'
import type { ReleaseOut, ReleaseSourceOut, UpgradeStatusOut } from '@/api/types'
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
  () => releaseApi.build(buildVersion.value.trim(), buildNotes.value.trim()),
  {
    success: (release) => `已构建并签发 ${release.version}（未铺开，Agent 还看不到）`,
    onDone: async () => {
      buildNotes.value = ''
      await refresh()
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
        <p class="page-hint">
          <strong>上传不等于铺开。</strong>必须点「铺开」，考试机才看得到这个版本。
        </p>
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
          <span>升级状态取不到时，别急着铺开新版本 —— 哪台机器升了、升到哪个版本都会对不上。</span>
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

      <p class="page-hint">没有私钥就签不出包，现在升级不了任何一台机器。</p>

      <el-descriptions :column="1" size="small" border style="margin-top: 12px">
        <el-descriptions-item label="第一步：生成密钥对">
          <span class="mono">
            python -m syncoj_server.cli genkey --out /etc/syncoj/release-key.pem
          </span>
          （私钥留在服务端，公钥默认落在同目录的 .pub.json）
        </el-descriptions-item>
        <el-descriptions-item label="第二步：服务端指向私钥">
          <span class="mono">SYNCOJ_RELEASE_KEY=/etc/syncoj/release-key.pem</span>
          ，然后重启服务端；本页会跟着变成可用状态
        </el-descriptions-item>
        <el-descriptions-item label="第三步：分发公钥">
          把公钥放到每台考试机的
          <span class="mono">/etc/syncoj/release-key.pub.json</span>
          ，并在 agent.ini 的 <span class="mono">[upgrade]</span> 段写
          <span class="mono">public_key =</span> 指向它 —— 少这一步机器会验签失败，
          表现和"没升级"一模一样
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

      <p class="page-hint">
        打出来的包直接进版本历史，但<strong>仍然是「未铺开」—— 要再点「铺开」，机器才拿得到。</strong>
      </p>

      <p v-if="versionDrifts" class="page-hint warn-hint">
        提交的版本号和源码里写的
        <span class="mono">{{ source?.version }}</span>
        不一致，服务端会拒绝；两边改成一样再提交。
      </p>
      <p v-else-if="versionExists" class="page-hint warn-hint">
        历史里已经有 {{ buildVersion.trim() }}：未铺开的同名版本会被覆盖，
        正在铺开的要先「撤回」。
      </p>

      <p v-if="trustAnchorMissing" class="page-hint warn-hint">
        本机没有要内嵌进包里的发布公钥（<span class="mono">.key/release-key.pub.json</span>），
        服务端会拒绝构建；跑一次 <span class="mono">syncoj-server init</span> 生成它。
      </p>
    </el-card>

    <el-card v-else-if="signingReady && source && !source.available" shadow="never" class="section">
      <template #header><span>发布当前版本</span></template>
      <p class="page-hint" style="margin: 0">
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
      <p class="page-hint">上传后服务端会用私钥签名，但<strong>传完仍是「未铺开」</strong>。</p>
      <p v-if="status?.key_id" class="page-hint">
        当前用于签名的密钥：<span class="mono">{{ status.key_id }}</span>
      </p>
    </el-card>

    <el-card v-if="activeRelease" shadow="never" class="section">
      <template #header><span>当前铺开版本（Agent 现在能拿到的就是它）</span></template>
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
      <p class="page-hint">客户端按 <code>upgrade.mode</code> 行动：<code>off</code> 只记录不下载；<code>stage</code> 下载并验签但不激活；<code>apply</code> 才切换并重启。</p>

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
        <p class="page-hint" style="margin-bottom: 0">
          这条命令会从服务端取安装器与 <span class="mono">{{ activeRelease.version }}</span>
          的包，并<strong>对照服务端台账校验 sha256</strong>。
        </p>
      </template>
      <p v-else class="page-hint warn-hint">
        这台服务端报不出自己对外的地址，所以给不出安装命令 ——
        配好 <span class="mono">SYNCOJ_PUBLIC_URL</span> 再回来看。
      </p>
    </el-card>

    <el-card v-else-if="signingReady && !loading" shadow="never" class="section">
      <template #header><span>当前没有铺开任何版本</span></template>
      <p class="page-hint" style="margin: 0">
        Agent 现在不会被提供任何升级包。上传一个版本再点「铺开」，它才会开始下发。
      </p>
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

        <el-table-column label="操作" width="220" fixed="right">
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

      <p class="page-hint">铺开中 {{ rolledOutCount }} 个，已撤回 {{ yankedCount }} 个。「删除」删的是发布记录，不是已经装到机器上的程序。</p>
    </el-card>

    <el-dialog v-model="editVisible" :title="`编辑版本 ${editForm.version} 的备注`" width="520px">
      <el-input
        v-model="editForm.notes"
        type="textarea"
        :rows="4"
        maxlength="2000"
        placeholder="例如：修复了扫描目录不存在时的崩溃"
      />
      <p class="page-hint">备注不参与签名 —— 改它不会让已铺开的版本失效。</p>
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

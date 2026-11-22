<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { CONFIG_POLICY_CHOICES, UPGRADE_MODE_CHOICES, releaseApi, bootstrapKeyApi } from '@/api'
import type {
  BootstrapKeyOut,
  ReleaseOut,
  ReleaseSourceOut,
  UpgradeStatusOut,
} from '@/api/types'
import PageShell from '@/components/PageShell.vue'
import SectionCard from '@/components/SectionCard.vue'
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
 * **默认关**：附带密钥的包等于一张能注册进这台服务端的通行证。铺开后装机入口
 * （`/api/v1/agent/install/*`，不鉴权）照常下发它，在线装机不必再给 `--bootstrap-key`；
 * 能访问公开端口的人都能取到这把密钥，装完机就吊销。
 * 警示文案写死在模板里，别抄成"建议"。
 */
const includeBootstrapKey = ref(false)
/**
 * 包内那把密钥要不要覆盖机器上已有的那一把。
 *
 * **默认 `replace`**：包内那把是权威。真机事故就是这条默认值的由来 —— 机器上
 * 躺着一把已吊销的旧钥，安装器按"已有一份就不动"跳过，那把新钥永远用不上，
 * 机器永远注册不上。教师仍可手动改回 `keep`（那台机器手工配过，别动它）。
 */
const bootstrapKeyPolicy = ref<'keep' | 'replace'>('replace')

/** 逐键策略。默认全部"不改" —— 升级包不该顺手改掉考场机器的配置。 */
const configPolicy = reactive<Record<string, 'keep' | 'default' | 'force'>>({})

/** 自更新模式。默认 `apply`：发出去就是为了让它生效。 */
const upgradeMode = ref<'apply' | 'stage' | 'off'>('apply')

/** 高级选项那一块折叠着 —— 三条策略都有默认值，多数发版不需要动它们。 */
const advancedActive = ref<string[]>([])

/**
 * 逐键策略的**分组视图**。
 *
 * 键名与默认值都从服务端来（`/releases/source`）：前端自己抄一份清单，漂的那天
 * 教师配了一条服务端不认识的键，表现是"策略没生效"，而界面上看不出哪里不对。
 */
const configPolicyGroups = computed(() => {
  const groups: { section: string; keys: { key: string; name: string }[] }[] = []
  for (const key of source.value?.config_policy_keys ?? []) {
    const [section, name] = key.split('.')
    let group = groups.find((item) => item.section === section)
    if (!group) {
      group = { section, keys: [] }
      groups.push(group)
    }
    group.keys.push({ key, name })
  }
  return groups
})

/** 把所有逐键策略恢复成"不改"，并把其它选项恢复成默认值。 */
function resetPolicies(): void {
  for (const key of source.value?.config_policy_keys ?? []) {
    configPolicy[key] = 'keep'
  }
  bootstrapKeyPolicy.value = 'replace'
  upgradeMode.value = 'apply'
  includeBootstrapKey.value = false
}

async function loadSource(): Promise<void> {
  try {
    source.value = await releaseApi.source()
    if (!buildVersion.value) buildVersion.value = source.value.version ?? ''
    resetPolicies()
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
    ElMessage.warning('复制失败，请手动复制')
  }
}

/**
 * 这些键是**机器自己的事实**（它连哪个服务端、以谁的身份跑、装在哪、信任哪把钥）。
 *
 * 逐键策略默认全"不改"本来就是对的，这里只是把它在界面上标出来：把一台机器改去
 * 连别的服务端、或者换掉它的运行账号，是**重装那台机器**该做的事，而不是发一次版
 * 顺手带过去的副作用。列表只影响提示，不影响能不能改 —— 真需要时教师仍然能改。
 */
const MACHINE_FACT_KEYS = [
  'server.url',
  'agent.run_user',
  'agent.state_dir',
  'agent.machine_id',
  'upgrade.install_root',
  'upgrade.public_key',
]

/**
 * 版本历史那一行里"改了几个配置键"。
 *
 * 只数**偏离默认**的：界面拿到的是补全过的完整映射，直接数条数会永远显示 20。
 */
function configTouchCount(release: ReleaseOut): number {
  return Object.values(release.config_policy ?? {}).filter((value) => value !== 'keep').length
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
      {
        // 没勾"附带密钥"时这一项无从谈起，只能是 keep —— 服务端也会拒 replace
        bootstrap_key_policy: includeBootstrapKey.value ? bootstrapKeyPolicy.value : 'keep',
        // 只把**偏离默认**的那些键发过去：留一堆 keep 只会让回执与审计难以阅读
        config_policy: Object.fromEntries(
          Object.entries(configPolicy).filter(([, value]) => value !== 'keep'),
        ),
        upgrade_mode: upgradeMode.value,
      },
    ),
  {
    success: (release) =>
      release.bootstrap_key_id
        ? `已签发 ${release.version}（附带密钥 ${release.bootstrap_key_label}，未铺开）`
        : `已签发 ${release.version}（未铺开）`,
    onDone: async (release) => {
      buildNotes.value = ''
      resetPolicies()
      await refresh()
      // 回执里的那一句"等于通行证"不能只写在页面上：发完版的人多半已经滚走了。
      // 但服务端**不返回明文**（库里只有哈希），能说的就是"哪一把、什么后果"。
      if (release.bootstrap_key_id) {
        ElMessage.warning({
          message:
            `${release.version} 附带统一注册密钥 ${release.bootstrap_key_label}：` +
            '持有安装包者均可注册机器，直到吊销该密钥。',
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
    success: (release) => `已上传 ${release.version}（未铺开）`,
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
    ElMessage.warning('请填写版本号')
    return
  }
  void upload.run(file)
}

// 两个动作都返回更新后的 ReleaseOut，所以回执里带上服务端算出来的版本号 ——
// 那是"这一下动的是哪个版本"的唯一权威答案
const rolloutMutation = useMutation((release: ReleaseOut) => releaseApi.rollout(release.id), {
  success: (result: ReleaseOut) => `已铺开 ${result.version}`,
  onDone: () => refresh(),
})

const yankMutation = useMutation((release: ReleaseOut) => releaseApi.yank(release.id), {
  success: (result: ReleaseOut) => `已撤回 ${result.version}`,
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
      `将向全部机器提供 ${release.version}` +
        (previous ? `；${previous.version} 将被撤回。` : '。'),
      '铺开版本？',
      { type: 'warning', confirmButtonText: '铺开', cancelButtonText: '取消' },
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
      `${release.version} 将不再提供给 Agent（含排队下载中的）；已升级的机器不会降级。`,
      '撤回版本？',
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
      `将删除 ${release.version} 的发布记录，不可恢复；已安装的 Agent 不受影响。`,
      '删除发布记录？',
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
      `已吊销 ${key.label || `#${key.id}`}`,
    onDone: () => refresh(),
  },
)

async function revokeKey(release: ReleaseOut): Promise<void> {
  if (!release.bootstrap_key_id) return
  try {
    await ElMessageBox.confirm(
      `将吊销 ${release.version} 附带的密钥 ${release.bootstrap_key_label ?? `#${release.bootstrap_key_id}`}，不可恢复；` +
        '用该安装包装机将无法注册，已注册的机器不受影响。',
      '吊销统一注册密钥？',
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
    success: () => '已保存',
    onDone: async () => {
      editVisible.value = false
      await refresh()
    },
  },
)
</script>

<template>
  <PageShell title="Agent 发布" :error="error" retryable @retry="refresh">
    <template #toolbar>
      <el-button :loading="loading" @click="refresh">
        <el-icon><Refresh /></el-icon>刷新
      </el-button>
    </template>

    <SectionCard v-if="signingKnown && !signingReady" title="发布签名私钥">
      <el-alert
        type="error"
        :closable="false"
        show-icon
        title="未配置发布签名私钥：无法签发与铺开版本"
        style="margin-bottom: 12px"
      />
      <el-descriptions :column="1" border>
        <el-descriptions-item label="生成密钥对">
          <span class="mono">python -m syncoj_server.cli genkey --out /etc/syncoj/release-key.pem</span>
        </el-descriptions-item>
        <el-descriptions-item label="服务端环境变量">
          <span class="mono">SYNCOJ_RELEASE_KEY=/etc/syncoj/release-key.pem</span>（重启服务端）
        </el-descriptions-item>
        <el-descriptions-item label="机器公钥">
          <span class="mono">/etc/syncoj/release-key.pub.json</span> ·
          agent.ini <span class="mono">[upgrade] public_key</span>
        </el-descriptions-item>
        <el-descriptions-item v-if="status?.error" label="错误">
          <span class="mono">{{ status.error }}</span>
        </el-descriptions-item>
      </el-descriptions>
    </SectionCard>

    <SectionCard v-if="activeRelease || (signingReady && !loading)" title="当前铺开版本">
      <template v-if="activeRelease" #extra>
        <el-button
          type="danger"
          plain
          :loading="yankMutation.pending.value"
          @click="yank(activeRelease)"
        >
          撤回
        </el-button>
      </template>
      <template v-if="activeRelease">
        <div class="active-box">
          <div class="active-version mono">{{ activeRelease.version }}</div>
          <div class="cell-sub">
            {{ formatBytes(activeRelease.size) }} · 发布于 {{ formatTime(activeRelease.published_at) }}
          </div>
        </div>
        <div v-if="installCommand" class="install-row">
          <span class="install-label">安装命令</span>
          <code class="install-command">{{ installCommand }}</code>
          <el-button @click="copyText(installCommand, '安装命令')">
            <el-icon><CopyDocument /></el-icon>复制
          </el-button>
        </div>
        <p v-else class="warn-hint">未配置对外地址（SYNCOJ_PUBLIC_URL），无安装命令</p>
      </template>
      <el-empty v-else description="无铺开版本" :image-size="64" />
    </SectionCard>

    <SectionCard v-if="signingReady && buildable" title="发布当前版本" :meta="source?.version ? `源码版本 ${source.version}` : ''">
      <div class="upload-row">
        <el-input v-model="buildVersion" placeholder="例如 0.1.1" style="width: 160px" />
        <el-input v-model="buildNotes" placeholder="发布说明（可选）" style="flex: 1; min-width: 200px" />
        <el-button
          type="primary"
          :loading="build.pending.value"
          :disabled="!canBuild"
          @click="build.run(undefined)"
        >
          <el-icon><Stamp /></el-icon>构建并签发
        </el-button>
      </div>

      <p v-if="versionDrifts" class="warn-hint">版本号与源码不一致，服务端将拒绝</p>
      <p v-else-if="versionExists" class="warn-hint">版本号已存在</p>
      <p v-if="trustAnchorMissing" class="warn-hint">缺发布公钥，无法构建</p>

      <div class="key-option">
        <el-checkbox v-model="includeBootstrapKey" :disabled="!canBuild">附带统一注册密钥</el-checkbox>
        <span v-if="includeBootstrapKey" class="danger-hint">
          持有安装包者均可注册机器，直到吊销该密钥。
        </span>
      </div>

      <el-collapse v-model="advancedActive" class="advanced">
        <el-collapse-item name="policy" title="安装策略">
          <div v-if="includeBootstrapKey" class="policy-row">
            <span class="policy-label">机器上已有的注册密钥</span>
            <el-radio-group v-model="bootstrapKeyPolicy" :disabled="!canBuild">
              <el-radio value="replace">覆盖（原密钥备份）</el-radio>
              <el-radio value="keep">保留</el-radio>
            </el-radio-group>
          </div>

          <div class="policy-row">
            <span class="policy-label">升级模式</span>
            <el-radio-group v-model="upgradeMode" :disabled="!canBuild">
              <el-radio v-for="item in UPGRADE_MODE_CHOICES" :key="item.value" :value="item.value">
                {{ item.label }}
              </el-radio>
            </el-radio-group>
          </div>

          <div class="policy-row">
            <span class="policy-label">agent.ini 逐键更新</span>
            <div v-for="group in configPolicyGroups" :key="group.section" class="policy-group">
              <div class="policy-section mono">[{{ group.section }}]</div>
              <div v-for="item in group.keys" :key="item.key" class="policy-key">
                <span class="mono policy-key-name">{{ item.name }}</span>
                <el-radio-group v-model="configPolicy[item.key]" :disabled="!canBuild">
                  <el-radio-button
                    v-for="choice in CONFIG_POLICY_CHOICES"
                    :key="choice.value"
                    :value="choice.value"
                  >
                    {{ choice.label }}
                  </el-radio-button>
                </el-radio-group>
                <el-tag v-if="MACHINE_FACT_KEYS.includes(item.key)" type="warning" effect="plain">
                  机器自有
                </el-tag>
              </div>
            </div>
          </div>
        </el-collapse-item>
      </el-collapse>
    </SectionCard>

    <SectionCard v-else-if="signingReady && source && !source.available" title="发布当前版本">
      <span class="muted">{{ source.reason }}</span>
    </SectionCard>

    <SectionCard
      v-if="signingReady"
      title="上传新版本"
      :meta="status?.key_id ? `签名密钥 ${status.key_id}` : ''"
    >
      <div class="upload-row">
        <el-input v-model="version" placeholder="例如 0.1.1" style="width: 160px" />
        <el-input v-model="notes" placeholder="发布说明（可选）" style="flex: 1; min-width: 200px" />
        <input ref="fileInput" type="file" style="display: none" @change="handleFileChange" />
        <el-button :loading="upload.pending.value" @click="pickFile">
          <el-icon><Upload /></el-icon>选择安装包并上传
        </el-button>
      </div>
    </SectionCard>

    <SectionCard
      title="版本历史"
      :meta="`${releases.length} 个 · 铺开中 ${rolledOutCount} · 已撤回 ${yankedCount}`"
      flush
    >
      <el-table v-loading="loading" :data="releases">
        <el-table-column label="版本" prop="version" width="120">
          <template #default="{ row }">
            <span class="mono">{{ row.version }}</span>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="statusTag(row).type" effect="light">{{ statusTag(row).text }}</el-tag>
          </template>
        </el-table-column>

        <el-table-column label="大小" width="100" align="right">
          <template #default="{ row }">{{ formatBytes(row.size) }}</template>
        </el-table-column>

        <el-table-column label="SHA-256" width="150">
          <template #default="{ row }">
            <el-tooltip :content="row.sha256">
              <span class="mono muted">{{ row.sha256.slice(0, 12) }}…</span>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="附带密钥" min-width="160">
          <template #default="{ row }">
            <span v-if="!row.bootstrap_key_id" class="muted">—</span>
            <el-tag v-else :type="row.bootstrap_key_revoked ? 'info' : 'danger'" effect="plain">
              {{ row.bootstrap_key_label || `#${row.bootstrap_key_id}` }}
              {{ row.bootstrap_key_revoked ? '（已吊销）' : '（有效）' }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="安装策略" min-width="180">
          <template #default="{ row }">
            <el-tag effect="plain">{{ row.upgrade_mode }}</el-tag>
            <span v-if="row.bootstrap_key_policy === 'replace'" class="cell-sub"> · 覆盖注册密钥</span>
            <span v-if="configTouchCount(row)" class="cell-sub">
              · {{ configTouchCount(row) }} 个配置键
            </span>
          </template>
        </el-table-column>

        <el-table-column label="说明" min-width="180">
          <template #default="{ row }">
            <span>{{ row.notes || '—' }}</span>
          </template>
        </el-table-column>

        <el-table-column label="上传时间" width="170">
          <template #default="{ row }">
            <span class="cell-sub">{{ formatTime(row.created_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="240" fixed="right">
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
            <el-button link type="primary" size="small" @click="openEdit(row)">备注</el-button>
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
          </template>
        </el-table-column>

        <template #empty>无版本</template>
      </el-table>
    </SectionCard>

    <el-dialog v-model="editVisible" :title="`编辑备注：${editForm.version}`" width="520px">
      <el-input
        v-model="editForm.notes"
        type="textarea"
        :rows="4"
        maxlength="2000"
        placeholder="例如 修复扫描目录不存在时的崩溃"
      />
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="editSave.pending.value" @click="editSave.run(undefined)">
          保存
        </el-button>
      </template>
    </el-dialog>
  </PageShell>
</template>

<style scoped>
.upload-row {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}

.active-box {
  display: flex;
  align-items: baseline;
  gap: 16px;
  flex-wrap: wrap;
  padding: 12px 16px;
  background: var(--sj-success-soft);
  border: 1px solid var(--sj-border);
  border-radius: var(--sj-radius-sm);
}

.active-version {
  font-size: 22px;
  font-weight: 700;
  color: var(--sj-success);
  line-height: 1.2;
}

.muted {
  color: var(--sj-text-3);
}

.warn-hint {
  margin: 8px 0 0;
  font-size: 12.5px;
  color: var(--sj-warning);
}

.danger-hint {
  margin-left: 12px;
  font-size: 12.5px;
  color: var(--sj-danger);
}

.key-option {
  margin-top: 12px;
}

.advanced {
  margin-top: 4px;
}

.policy-row {
  margin-bottom: 14px;
}

.policy-label {
  display: block;
  font-size: 13px;
  color: var(--sj-text-2);
  margin-bottom: 4px;
}

.policy-group {
  margin-top: 8px;
}

.policy-section {
  font-size: 12px;
  color: var(--sj-text-3);
  margin-bottom: 4px;
}

.policy-key {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 4px;
}

.policy-key-name {
  width: 150px;
  font-size: 12px;
}

.install-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  flex-wrap: wrap;
}

.install-label {
  font-size: 13px;
  color: var(--sj-text-2);
}

/* 安装命令不能截断：复制出去必须是完整的一条 */
.install-command {
  flex: 1;
  min-width: 280px;
  padding: 6px 10px;
  color: var(--sj-text);
  background: var(--sj-surface-2);
  border: 1px solid var(--sj-border);
  border-radius: var(--sj-radius-sm);
  font-family: var(--sj-mono);
  font-size: 12px;
  line-height: 18px;
  word-break: break-all;
  white-space: pre-wrap;
}
</style>

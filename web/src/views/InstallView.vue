<script setup lang="ts">
/**
 * 装机页（**免登录**）。
 *
 * 使用者是抱着笔记本站在机房过道里的教师，所以这一页只干一件事：给出一条能直接
 * 抄到空机器上跑的命令。它免登录是因为**空机器上什么都没有** —— 要求登录就等于
 * 没有装机入口；也因此这一页只报"本来就该发给每台机器"的东西（版本、包体、
 * 校验和），不含任何考场数据。
 *
 * 与 LoginView 一样是顶层独立页面：套上管理端外壳会让人以为要登录，
 * 而外壳里的菜单点下去全都会跳回登录页。
 */

import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { absolute, bootstrapCommand, installApi } from '@/api'
import { ApiError } from '@/api/client'
import { describeError } from '@/api/crud'
import { apiOrigin } from '@/publicMode'
import { formatBytes } from '@/utils/format'

/**
 * 台账形状从接口签名推出来，不在这里手抄一份。
 * 服务端模型是唯一真相源（见 DESIGN §5.3），抄一份的下场是"两边慢慢不一致，
 * 而界面上看起来一切正常"。
 */
type Ledger = Awaited<ReturnType<typeof installApi.ledger>>

const router = useRouter()

const ledger = ref<Ledger | null>(null)
const loading = ref(false)
/** 取数失败（网络、服务端 500）—— 要可行动，所以配一个重试按钮。 */
const error = ref<string | null>(null)
/** 服务端说"还没有铺开任何版本"—— 那是**正常状态**，不是故障，单独一个分支。 */
const emptyReason = ref<string | null>(null)

/**
 * 拉当前可装机版本的台账。
 *
 * **404 不是错误**：它表示这台服务端还没有铺开任何 Agent 版本，服务端在 `detail`
 * 里已经写清了该怎么办。所以这里把它翻成空状态，把那句话原样显示（不加工 ——
 * 加工过的提示反而会漏掉服务端想说的那一步）。其它失败才是真错误。
 */
async function load(): Promise<void> {
  loading.value = true
  try {
    ledger.value = await installApi.ledger()
    emptyReason.value = null
    error.value = null
  } catch (err) {
    ledger.value = null
    if (err instanceof ApiError && err.status === 404) {
      emptyReason.value = describeError(err)
      error.value = null
    } else {
      error.value = describeError(err)
      emptyReason.value = null
    }
  } finally {
    loading.value = false
  }
}

onMounted(load)

/**
 * 命令里的地址取**当前页面的 origin**：教师是从某个地址打开这一页的，那个地址
 * 就是从局域网真的可达的地址。服务端自己算不出这个（多网卡、反向代理），
 * 而它报绝对地址必然有一半是错的。
 */
const origin = window.location.origin

/**
 * 但**命令**里那个地址还要多加一层：它会被写进那台机器、从此长期使用，所以取
 * API 端口（`apiOrigin()`），而不是被打开的这一页所在的端口。教师从公开端口
 * （默认 80）打开装机页时，两者不是同一个地址 —— 拿页面的 origin 会把这台机器
 * 永久指到一个"方便用的"端口上，将来那个端口一关，机器就找不着服务端了。
 *
 * 下面的下载链接留在当前 origin：那是**现在这个浏览器**马上就能下到的地址，
 * 公开端口上这两个文件也是发得出去的。
 */
const command = computed(() => bootstrapCommand(apiOrigin()))
/** 服务端给的是相对路径，拼成绝对地址才能给浏览器下载 —— 拼法由 api 层统一提供。 */
const bundleUrl = computed(() => (ledger.value ? absolute(origin, ledger.value.bundle) : ''))
const installerUrl = computed(() =>
  ledger.value ? absolute(origin, ledger.value.installer) : '',
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
 * 没有版本可装时唯一能做的动作是去铺开一个，而「铺开」要求管理员登录。
 *
 * 所以按钮文案与行为必须一致：写「去登录后到 Agent 发布页铺开」，
 * 点它就带 `redirect=/releases` 去登录（登录成功会直接落回 Agent 发布页）。
 * 写成"去 Agent 发布"再跳到登录页，教师会以为是自己点错了。
 */
function goRollout(): void {
  void router.push({ name: 'login', query: { redirect: '/releases' } })
}
</script>

<template>
  <div class="install-page">
    <el-card v-loading="loading" class="install-card" shadow="always">
      <div class="brand">
        <div class="brand-title">把 SyncOJ 装到考试机上</div>
      </div>

      <!--
        取数失败：命令里的版本号与校验和都可能对不上，所以先说"别抄"再给重试。
      -->
      <el-alert v-if="error" type="error" :closable="false" show-icon :title="error">
        <div class="alert-row">
          <span>取不到台账就先别抄命令 —— 版本号和校验和都可能对不上。</span>
          <el-button size="small" type="primary" plain @click="load">重试</el-button>
        </div>
      </el-alert>

      <!--
        没有铺开任何版本：不显示命令、也不显示下载区 —— 命令指向的包不存在，
        给出来只会让人在空机器上撞一个 404，然后怀疑网络。
      -->
      <div v-else-if="emptyReason" class="empty-state">
        <p class="empty-text">{{ emptyReason }}</p>
        <el-button type="primary" @click="goRollout">去登录后到 Agent 发布页铺开</el-button>
      </div>

      <template v-else-if="ledger">
        <!--
          主操作：一条命令。它必须是这一页最显眼的东西 —— 教师就是来抄它的。
          **不能截断**：被省略号吃掉尾巴的命令抄过去就是语法错误，而报错会指向
          shell 的哪一行，指不到"这里少了一段"。
        -->
        <div class="install-row">
          <code class="install-command">{{ command }}</code>
          <el-button size="small" type="primary" @click="copyText(command, '安装命令')">
            复制
          </el-button>
        </div>
        <p class="hint">在每台空机器上跑它。</p>

        <div class="info">
          <div class="info-row">
            <span class="info-label">版本</span>
            <span class="mono">{{ ledger.version }}</span>
            <!--
              签名有没有是这一页唯一会让人误判的字段：没签名时"机器有公钥也验不了"，
              所以不摆一个空的占位，直接标出来并把原因挂在 tooltip 上。
            -->
            <el-tooltip
              :content="
                ledger.signature
                  ? `已签名（key_id: ${ledger.key_id ?? '未知'}）`
                  : '台账里没有签名记录：机器上就算有发布公钥，也只能验 sha256'
              "
            >
              <el-tag :type="ledger.signature ? 'success' : 'warning'" size="small" effect="plain">
                {{ ledger.signature ? '已签名' : '未签名' }}
              </el-tag>
            </el-tooltip>
          </div>

          <div class="info-row">
            <span class="info-label">大小</span>
            <span>{{ formatBytes(ledger.size) }}</span>
          </div>

          <div class="info-row">
            <span class="info-label">sha256</span>
            <!-- 只显示前 12 位是为了扫一眼就能比较；全量在 tooltip 里，供人手工核对 -->
            <el-tooltip :content="ledger.sha256">
              <span class="mono">{{ ledger.sha256.slice(0, 12) }}…</span>
            </el-tooltip>
            <el-button link type="primary" @click="copyText(ledger.sha256, '完整 sha256')">
              复制
            </el-button>
          </div>

          <div class="info-row">
            <span class="info-label">发布说明</span>
            <span class="notes">{{ ledger.notes || '—' }}</span>
          </div>
        </div>

        <!--
          下载区：给"想先下下来、再用 U 盘拷过去"的场景。address 用当前 origin 拼，
          理由与安装命令一样（只有这个地址是局域网真的可达的）。
        -->
        <div class="download-row">
          <span class="info-label">先下后拷</span>
          <el-link type="primary" :href="bundleUrl" target="_blank" rel="noopener">
            下载安装包
          </el-link>
          <el-link type="primary" :href="installerUrl" target="_blank" rel="noopener">
            下载安装器
          </el-link>
        </div>

        <!--
          一句实话，不展开：sha256 与包体来自同一个未鉴权的地方，它挡的是传输损坏，
          挡不住"有人替你换了个包"——那必须靠机器上的发布公钥验签。
        -->
        <p class="hint">
          sha256 只挡传输损坏；要挡住"有人替你换了个包"，得让机器上有发布公钥。
        </p>
        <!--
          两件事必须写在同一条路上：机器**先要有统一注册密钥**才会注册、才会写出配对码。
          少了前半句，教师会站在机器前一直等一个永远不出现的东西 —— 而安装器那时只会说
          "没有统一密钥，装不出注册单元"，那句话在几十行输出里很容易被忽略。
        -->
        <p class="hint">
          机器上要先有统一注册密钥（<code>/etc/syncoj/bootstrap.key</code>，见「机器配对 →
          装机设置」；或者用一个勾了"附带密钥"的版本），否则它不会注册。
        </p>
        <p class="hint">装完机器会在桌面写出六位配对码，去管理界面「机器配对」绑给选手。</p>
      </template>

      <!-- 首屏：台账还没回来，给骨架而不是一片空白（空白会被当成"这页坏了"） -->
      <el-skeleton v-else-if="loading" :rows="4" animated />
    </el-card>
  </div>
</template>

<style scoped>
.install-page {
  min-height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px 16px;
  background: linear-gradient(135deg, #1f2d3d 0%, #2c405a 100%);
}

.install-card {
  width: 720px;
  max-width: 100%;
  border-radius: 8px;
}

.brand {
  margin-bottom: 16px;
}

.brand-title {
  font-size: 22px;
  font-weight: 700;
  color: #1f2d3d;
}

.brand-sub {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
}

.hint {
  margin: 8px 0 0;
  font-size: 12px;
  color: #909399;
  line-height: 1.7;
}

.alert-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.empty-state {
  padding: 24px 0 8px;
  text-align: center;
}

.empty-text {
  margin: 0 0 16px;
  font-size: 13px;
  color: #606266;
  line-height: 1.7;
}

.install-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

/*
  命令本体：等宽 + 换行。

  **用 `overflow-wrap: anywhere`，不用 `word-break: break-all`。** 后者连单词中间都
  断：实测把 `--server` 拆成了 `--serve` + `r` 两行，而这一行是给**站在机器前面照抄**
  的人看的（剪贴板被策略禁掉、或者要手敲时）。`anywhere` 优先在空格处断，只有单个词
  真的长过一行时才断词 —— 而那种"长词"通常是 URL，断在哪里都还认得出来。

  `white-space: pre-wrap` 保留换行与连续空格；`min-width: 0` 让它在 flex 里能收缩
  （否则 flex 默认的 `min-width: auto` 会撑出横向滚动条）。
*/
.install-command {
  flex: 1;
  min-width: 0;
  padding: 8px 10px;
  background: #f5f7fa;
  border: 1px solid #e4e7ed;
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace;
  font-size: 12px;
  line-height: 18px;
  color: #303133;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.info {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid #ebeef5;
}

.info-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 26px;
  flex-wrap: wrap;
}

.info-label {
  flex: none;
  width: 64px;
  font-size: 12px;
  color: #909399;
}

.notes {
  font-size: 13px;
  line-height: 1.6;
}

.download-row {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid #ebeef5;
}
</style>

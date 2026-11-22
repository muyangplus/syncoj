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

import { absolute, bootstrapCommand, installApi, uninstallCommand } from '@/api'
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

/**
 * 卸载命令也在这里给出，理由与安装命令一样：教师是**站在这台机器前面**打开这一页的。
 * 而它必须能从这一页拿到，是因为离线包刻意不含安装器 —— 装完之后机器上只剩
 * `run_agent.py`，没有 install.py，手工卸载的第一步就得先把安装器弄回去。
 *
 * 地址同样取 API 端口：这是**现在**要下载 bootstrap.sh 的地址，不是被写进那台
 * 机器长期使用的地址（卸载是一次性的）。
 */
const uninstall = computed(() => uninstallCommand(apiOrigin()))
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
    ElMessage.warning('复制失败，请手工选取')
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
    <div class="install-shell">
      <header class="brand">
        <span class="brand-mark">S</span>
        <div>
          <div class="brand-title">SyncOJ 装机</div>
          <div class="brand-sub">考试机 Agent</div>
        </div>
      </header>

      <section v-loading="loading" class="panel">
        <!-- 取数失败：版本与校验和都不可信，只给重试 -->
        <el-alert v-if="error" type="error" :closable="false" show-icon :title="error">
          <div class="alert-row">
            <el-button type="primary" plain @click="load">重试</el-button>
          </div>
        </el-alert>

        <!-- 未铺开任何版本：不给命令与下载（指向的包不存在） -->
        <div v-else-if="emptyReason" class="empty-state">
          <el-icon class="empty-icon"><Box /></el-icon>
          <p class="empty-text">{{ emptyReason }}</p>
          <el-button type="primary" @click="goRollout">登录并铺开版本</el-button>
        </div>

        <template v-else-if="ledger">
          <!-- 主操作：一条命令，不能截断 -->
          <div class="block-label">安装命令</div>
          <div class="command-box">
            <code class="install-command">{{ command }}</code>
            <el-button type="primary" @click="copyText(command, '安装命令')">
              <el-icon><DocumentCopy /></el-icon>复制
            </el-button>
          </div>

          <dl class="info">
            <div class="info-row">
              <dt>版本</dt>
              <dd>
                <span class="mono">{{ ledger.version }}</span>
                <el-tooltip :disabled="!ledger.signature" :content="`key_id: ${ledger.key_id ?? '—'}`">
                  <el-tag :type="ledger.signature ? 'success' : 'warning'" size="small" effect="light">
                    {{ ledger.signature ? '已签名' : '未签名' }}
                  </el-tag>
                </el-tooltip>
              </dd>
            </div>
            <div class="info-row">
              <dt>大小</dt>
              <dd>{{ formatBytes(ledger.size) }}</dd>
            </div>
            <div class="info-row">
              <dt>sha256</dt>
              <dd>
                <el-tooltip :content="ledger.sha256">
                  <span class="mono">{{ ledger.sha256.slice(0, 12) }}…</span>
                </el-tooltip>
                <el-button link type="primary" @click="copyText(ledger.sha256, '完整 sha256')">复制</el-button>
              </dd>
            </div>
            <div class="info-row">
              <dt>发布说明</dt>
              <dd class="notes">{{ ledger.notes || '—' }}</dd>
            </div>
            <div class="info-row">
              <dt>离线下载</dt>
              <dd class="downloads">
                <el-link type="primary" :href="bundleUrl" target="_blank" rel="noopener">
                  <el-icon><Download /></el-icon>安装包
                </el-link>
                <el-link type="primary" :href="installerUrl" target="_blank" rel="noopener">
                  <el-icon><Download /></el-icon>安装器
                </el-link>
              </dd>
            </div>
          </dl>
        </template>

        <el-skeleton v-else-if="loading" :rows="4" animated />
      </section>

      <!-- 卸载与台账无关，始终显示；次要且破坏性，放最下 -->
      <section class="panel panel-muted">
        <div class="block-label">卸载命令</div>
        <div class="command-box">
          <code class="install-command">{{ uninstall }}</code>
          <el-button @click="copyText(uninstall, '卸载命令')">
            <el-icon><DocumentCopy /></el-icon>复制
          </el-button>
        </div>
        <p class="consequence">将卸载 Agent，并删除本机的统一注册密钥与凭据。</p>
      </section>
    </div>
  </div>
</template>

<style scoped>
.install-page {
  min-height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 32px 16px;
  background:
    radial-gradient(60% 40% at 50% 0%, var(--sj-primary-soft) 0%, transparent 70%),
    var(--sj-bg);
}

.install-shell {
  width: 100%;
  max-width: 760px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 20px;
}

/* 与 AppLayout 侧栏的品牌方块一致 */
.brand-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  flex: none;
  border-radius: 11px;
  background: var(--sj-brand-gradient);
  color: var(--el-color-white);
  font-weight: 700;
  font-size: 18px;
}

.brand-title {
  font-size: 20px;
  font-weight: 700;
  letter-spacing: -0.01em;
  color: var(--sj-text);
}

.brand-sub {
  font-size: 12.5px;
  color: var(--sj-text-3);
}

.panel {
  padding: 22px 24px;
  background: var(--sj-surface);
  border: 1px solid var(--sj-border);
  border-radius: var(--sj-radius);
  box-shadow: var(--sj-shadow-lg);
}

.panel + .panel {
  margin-top: 16px;
}

.panel-muted {
  box-shadow: var(--sj-shadow);
}

.block-label {
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--sj-text-2);
}

.alert-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 6px;
}

.empty-state {
  padding: 24px 0 8px;
  text-align: center;
}

.empty-icon {
  font-size: 32px;
  color: var(--sj-text-3);
}

.empty-text {
  margin: 10px 0 16px;
  font-size: 13px;
  color: var(--sj-text-2);
  line-height: 1.7;
}

.command-box {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}

/*
  命令本体：等宽 + 换行。用 overflow-wrap: anywhere（优先在空格处断），
  不用 word-break: break-all（会把 --server 拆成两行，照抄时出错）。
*/
.install-command {
  flex: 1;
  min-width: 0;
  padding: 10px 12px;
  background: var(--sj-surface-2);
  border: 1px solid var(--sj-border);
  border-radius: var(--sj-radius-sm);
  font-family: var(--sj-mono);
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--sj-text);
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.info {
  margin: 20px 0 0;
  padding-top: 14px;
  border-top: 1px solid var(--sj-border);
}

.info-row {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 32px;
}

.info-row dt {
  flex: none;
  width: 72px;
  font-size: 12.5px;
  color: var(--sj-text-3);
}

.info-row dd {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
  margin: 0;
  color: var(--sj-text);
}

.notes {
  font-size: 13px;
  line-height: 1.6;
}

.downloads {
  gap: 16px !important;
}

.downloads .el-icon {
  margin-right: 4px;
}

.consequence {
  margin: 10px 0 0;
  font-size: 12.5px;
  color: var(--sj-danger);
}

@media (max-width: 560px) {
  .command-box {
    flex-direction: column;
    align-items: stretch;
  }
}
</style>

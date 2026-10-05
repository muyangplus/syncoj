<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { releaseApi } from '@/api'
import type { ReleaseOut, UpgradeStatusOut } from '@/api/types'
import { usePolling } from '@/composables/usePolling'
import { formatBytes, formatTime } from '@/utils/format'

const status = ref<UpgradeStatusOut | null>(null)
const loading = ref(false)
const uploading = ref(false)
const error = ref<string | null>(null)

const fileInput = ref<HTMLInputElement>()
const version = ref('')
const notes = ref('')

const activeRelease = computed(() => status.value?.active_release ?? null)
const signingReady = computed(() => status.value?.signing_available ?? false)
/** 生成的类型里 `releases` 是可选的（pydantic default_factory 不算 required），这里补一次默认值 */
const releases = computed<ReleaseOut[]>(() => status.value?.releases ?? [])

async function refresh(): Promise<void> {
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

function pickFile(): void {
  fileInput.value?.click()
}

async function handleFileChange(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  if (!version.value.trim()) {
    ElMessage.warning('请先填写版本号，再选择安装包')
    return
  }

  uploading.value = true
  try {
    const release = await releaseApi.upload(file, version.value.trim(), notes.value.trim())
    ElMessage.success(`已上传版本 ${release.version}（尚未铺开）`)
    version.value = ''
    notes.value = ''
    await refresh()
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    uploading.value = false
  }
}

async function rollout(release: ReleaseOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `开始向所有考试机提供版本 ${release.version}？\n\n` +
        '客户端只会下载并校验签名，是否真正应用取决于每台机器的 upgrade.mode 配置。\n' +
        'mode=off（默认）的机器只会记录"有新版本"，不会自动升级。',
      '铺开版本',
      { type: 'warning', confirmButtonText: '开始铺开', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await releaseApi.rollout(release.id)
    ElMessage.success(`已开始铺开 ${release.version}`)
    await refresh()
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

async function yank(release: ReleaseOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `撤回版本 ${release.version}？\n\n` +
        '撤回只能阻止影响面继续扩大，**已经升级过的机器不会自动降级** —— ' +
        '那需要客户端侧的回滚机制（升级后连续启动失败会自动回滚上一版本）。',
      '撤回版本',
      { type: 'warning', confirmButtonText: '撤回', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await releaseApi.yank(release.id)
    ElMessage.success(`已撤回 ${release.version}`)
    await refresh()
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

function statusTag(release: ReleaseOut): { text: string; type: 'success' | 'info' | 'warning' } {
  if (release.yanked) return { text: '已撤回', type: 'info' }
  if (release.rolled_out) return { text: '铺开中', type: 'success' }
  return { text: '未铺开', type: 'warning' }
}

// 编辑备注：上传时可能先空着，事后补上"这个版本修了什么"，
// 出问题时回看历史才有线索
const editVisible = ref(false)
const editSaving = ref(false)
const editForm = reactive({ id: 0, version: '', notes: '' })

function openEdit(release: ReleaseOut): void {
  editForm.id = release.id
  editForm.version = release.version
  editForm.notes = release.notes ?? ''
  editVisible.value = true
}

async function submitEdit(): Promise<void> {
  editSaving.value = true
  try {
    await releaseApi.update(editForm.id, { notes: editForm.notes.trim() || null })
    ElMessage.success('已保存')
    editVisible.value = false
    await refresh()
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    editSaving.value = false
  }
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h2 class="page-title">Agent 发布</h2>
        <p class="page-hint">
          <strong>上传不等于铺开。</strong>
          上传后 Agent 完全看不到这个版本，必须点「铺开」才会开始提供给考试机。
          同一时刻只允许一个版本处于铺开状态。
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
    />

    <el-alert
      v-if="status && !signingReady"
      type="warning"
      :closable="false"
      show-icon
      title="未配置发布签名私钥，自更新功能整体关闭"
      style="margin-bottom: 12px"
    >
      <template #default>
        <p style="margin: 4px 0">
          没有私钥就签不出包，而没有签名的包 Agent 一律拒绝 —— 这是安全的默认状态。
        </p>
        <p style="margin: 4px 0">
          启用方式：<code>syncoj-server genkey --out /etc/syncoj/release-key.pem</code>，
          启动服务端时设置环境变量 <code>SYNCOJ_RELEASE_KEY</code> 指向它，
          并把生成的公钥分发到每台考试机的 <code>/etc/syncoj/release-key.pub.json</code>。
        </p>
        <p v-if="status?.error" class="muted" style="margin: 4px 0">
          当前错误：{{ status.error }}
        </p>
      </template>
    </el-alert>

    <el-card v-if="signingReady" shadow="never" class="section">
      <template #header><span>上传新版本</span></template>
      <div class="upload-row">
        <el-input v-model="version" placeholder="版本号，例如 0.1.1" style="width: 160px" />
        <el-input v-model="notes" placeholder="发布说明（可选）" style="flex: 1" />
        <input ref="fileInput" type="file" style="display: none" @change="handleFileChange" />
        <el-button :loading="uploading" @click="pickFile">选择安装包并上传</el-button>
      </div>
      <p class="page-hint">
        安装包由 <code>agent/packaging/build_bundle.py</code> 生成。
        上传后服务端会用私钥对包的 sha256 签名；客户端先校验摘要再验签。
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
        <el-button type="danger" plain size="small" @click="yank(activeRelease)">撤回</el-button>
      </div>
      <p class="page-hint" style="margin-top: 8px">
        客户端的 <code>upgrade.mode</code> 决定它对这个版本做什么：
        <code>off</code>（默认）只记录不下载；<code>stage</code> 下载并验签但不激活；
        <code>apply</code> 才会切换软链并重启。
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

        <el-table-column label="说明" min-width="200">
          <template #default="{ row }">
            <span>{{ row.notes || '—' }}</span>
          </template>
        </el-table-column>

        <el-table-column label="上传时间" width="160">
          <template #default="{ row }">
            <span class="cell-sub">{{ formatTime(row.created_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="160" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="!row.rolled_out"
              link
              type="primary"
              size="small"
              @click="rollout(row)"
            >
              铺开
            </el-button>
            <el-button v-else link type="danger" size="small" @click="yank(row)">撤回</el-button>
            <el-button link size="small" @click="openEdit(row)">备注</el-button>
          </template>
        </el-table-column>

        <template #empty>
          <div class="empty-block">还没有上传过任何 Agent 版本</div>
        </template>
      </el-table>
    </el-card>

    <el-dialog v-model="editVisible" :title="`编辑版本 ${editForm.version} 的备注`" width="520px">
      <el-input
        v-model="editForm.notes"
        type="textarea"
        :rows="4"
        maxlength="2000"
        placeholder="例如：修复了扫描目录不存在时的崩溃"
      />
      <p class="page-hint">
        备注只影响界面上显示什么，不参与签名或校验 —— 改它不会让已铺开的版本失效。
      </p>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="editSaving" @click="submitEdit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
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
</style>

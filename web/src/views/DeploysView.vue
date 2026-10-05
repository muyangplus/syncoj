<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'

import { assetApi, deployApi, playerApi } from '@/api'
import type { AssetOut, DeployTaskOut, PlayerOut } from '@/api/types'
import { usePolling } from '@/composables/usePolling'
import { useContestStore } from '@/stores/contest'
import { formatBytes, formatTime, percent } from '@/utils/format'

const contest = useContestStore()
const { currentId } = storeToRefs(contest)

const assets = ref<AssetOut[]>([])
const tasks = ref<DeployTaskOut[]>([])
const players = ref<PlayerOut[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const uploading = ref(false)

const createDialog = ref(false)
const submitting = ref(false)
const fileInput = ref<HTMLInputElement>()

const form = reactive({
  assetId: undefined as number | undefined,
  targetKind: 'all' as 'all' | 'player' | 'group',
  playerIds: [] as number[],
  targetGroup: '',
  destDir: 'exam',
  mode: 'overwrite' as 'overwrite' | 'skip_exist',
})

const groups = computed(() => {
  const set = new Set<string>()
  for (const player of players.value) {
    if (player.group_name) set.add(player.group_name)
  }
  return [...set]
})

/** 展开行里的逐选手进度。 */
const expanded = ref<DeployTaskOut[]>([])

async function refresh(): Promise<void> {
  if (!currentId.value) {
    assets.value = []
    tasks.value = []
    return
  }
  try {
    const [assetList, taskList, playerList] = await Promise.all([
      assetApi.list(currentId.value),
      deployApi.list(currentId.value),
      players.value.length ? Promise.resolve(players.value) : playerApi.list(currentId.value),
    ])
    assets.value = assetList
    tasks.value = taskList
    players.value = playerList
    error.value = null
  } catch (err) {
    error.value = (err as Error).message
  } finally {
    loading.value = false
  }
}

// 下发任务里可能有几百 MB 的文件在传，10 秒一刷足够且不打搅
usePolling(refresh, { interval: 10000 })

async function loadTargets(task: DeployTaskOut): Promise<void> {
  try {
    const detail = await deployApi.get(task.id)
    const index = expanded.value.findIndex((t) => t.id === task.id)
    if (index >= 0) expanded.value[index] = detail
    else expanded.value.push(detail)
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

function pickFile(): void {
  fileInput.value?.click()
}

async function handleFileChange(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = '' // 允许连续上传同一个文件
  if (!file || !currentId.value) return

  uploading.value = true
  try {
    const asset = await assetApi.upload(currentId.value, file)
    ElMessage.success(`已上传 ${asset.filename}（${formatBytes(asset.size)}）`)
    await refresh()
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    uploading.value = false
  }
}

function openCreate(asset?: AssetOut): void {
  form.assetId = asset?.id
  form.targetKind = 'all'
  form.playerIds = []
  form.targetGroup = groups.value[0] ?? ''
  form.destDir = 'exam'
  form.mode = 'overwrite'
  createDialog.value = true
}

async function submitCreate(): Promise<void> {
  if (!currentId.value || !form.assetId) {
    ElMessage.warning('请选择要下发的文件')
    return
  }
  if (form.targetKind === 'player' && form.playerIds.length === 0) {
    ElMessage.warning('请选择至少一名选手')
    return
  }
  if (form.targetKind === 'group' && !form.targetGroup) {
    ElMessage.warning('请选择分组')
    return
  }

  submitting.value = true
  try {
    const task = await deployApi.create(currentId.value, {
      asset_id: form.assetId,
      target_kind: form.targetKind,
      player_ids: form.playerIds,
      target_group: form.targetKind === 'group' ? form.targetGroup : null,
      // 服务端会剥掉末尾斜杠，这里原样传即可
      dest_dir: form.destDir.trim(),
      mode: form.mode,
    })
    ElMessage.success(`已创建下发任务，目标 ${task.total} 台`)
    createDialog.value = false
    await refresh()
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    submitting.value = false
  }
}

async function cancelTask(task: DeployTaskOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `取消下发「${task.filename}」？已经收到文件的机器不会回滚 —— ` +
        '要求客户端删文件是危险动作，可能误删教师自己放的东西。',
      '取消下发',
      { type: 'warning', confirmButtonText: '取消下发', cancelButtonText: '再想想' },
    )
  } catch {
    return
  }
  try {
    const result = await deployApi.cancel(task.id)
    ElMessage.success(result.detail ?? '已取消')
    await refresh()
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

async function retryTask(task: DeployTaskOut): Promise<void> {
  try {
    const result = await deployApi.retry(task.id)
    ElMessage.success(result.detail ?? '已重置')
    await refresh()
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

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
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h2 class="page-title">文件下发</h2>
        <p class="page-hint">
          上传题面、测试点等文件并按选手下发。同一份内容只占一份磁盘空间；
          客户端断点续传，中途断网不会重传。
        </p>
      </div>
      <div class="toolbar">
        <input ref="fileInput" type="file" style="display: none" @change="handleFileChange" />
        <el-button size="small" :loading="uploading" @click="pickFile">上传文件</el-button>
        <el-button size="small" type="primary" :disabled="!assets.length" @click="openCreate()">
          新建下发
        </el-button>
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

    <el-card shadow="never" class="section">
      <template #header>
        <span>可下发文件（{{ assets.length }}）</span>
      </template>
      <el-table :data="assets" size="small" border>
        <el-table-column label="文件名" prop="filename" min-width="200" />
        <el-table-column label="类型" prop="kind" width="110" />
        <el-table-column label="大小" width="110" align="right">
          <template #default="{ row }">{{ formatBytes(row.size) }}</template>
        </el-table-column>
        <el-table-column label="校验和" width="150">
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
        <el-table-column label="操作" width="110" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openCreate(row)">下发</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-block">还没有上传任何文件</div>
        </template>
      </el-table>
    </el-card>

    <el-card shadow="never" class="section">
      <template #header>
        <span>下发任务（{{ tasks.length }}）</span>
      </template>
      <el-table
        :data="tasks"
        size="small"
        border
        row-key="id"
        @expand-change="(row: DeployTaskOut) => loadTargets(row)"
      >
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="expand-body">
              <el-table :data="row.targets" size="small">
                <el-table-column label="选手" prop="player_no" width="120" />
                <el-table-column label="状态" width="110">
                  <template #default="scope">
                    <el-tag :type="statusType(scope.row.status)" size="small" effect="plain">
                      {{ statusLabel(scope.row.status) }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="已传字节" width="130" align="right">
                  <template #default="scope">{{ formatBytes(scope.row.bytes_done) }}</template>
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
              <p v-if="!row.targets?.length" class="page-hint">展开后才会加载逐选手进度…</p>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="文件" prop="filename" min-width="180" />

        <el-table-column label="目标" width="150">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">
              {{ row.target_kind === 'all' ? '全员' : row.target_kind === 'group' ? '按分组' : '指定选手' }}
            </el-tag>
            <div class="cell-sub">目录：{{ row.dest_dir || '（根）' }}</div>
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

        <el-table-column label="操作" width="130" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="row.status !== 'done' && row.status !== 'cancelled'"
              link
              type="danger"
              size="small"
              @click="cancelTask(row)"
            >
              取消
            </el-button>
            <el-button
              v-if="row.failed > 0"
              link
              type="primary"
              size="small"
              @click="retryTask(row)"
            >
              重试
            </el-button>
          </template>
        </el-table-column>

        <template #empty>
          <div class="empty-block">还没有任何下发任务</div>
        </template>
      </el-table>
    </el-card>

    <el-dialog v-model="createDialog" title="新建下发任务" width="560px">
      <el-form label-width="90px">
        <el-form-item label="下发文件">
          <el-select v-model="form.assetId" placeholder="选择文件" style="width: 100%">
            <el-option
              v-for="asset in assets"
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

        <el-form-item label="目标目录">
          <el-input v-model="form.destDir" placeholder="例如 exam（相对客户端落地根目录）" />
          <div class="page-hint">
            相对于客户端配置里的 <code>deploy_root</code>。留空表示直接放在根目录。
            不接受绝对路径与 <code>..</code>。
          </div>
        </el-form-item>

        <el-form-item label="同名文件">
          <el-radio-group v-model="form.mode">
            <el-radio value="overwrite">覆盖</el-radio>
            <el-radio value="skip_exist">已存在则跳过</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="createDialog = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.section {
  margin-bottom: 12px;
}

.expand-body {
  padding: 8px 0 4px 42px;
}

.error-text {
  color: #f56c6c;
  font-size: 12px;
}
</style>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import { contestApi, rosterApi } from '@/api'
import type { ContestOut, RosterOut } from '@/api/types'

const props = defineProps<{
  modelValue: boolean
  contest: ContestOut | null
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  saved: [contest: ContestOut]
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const submitting = ref(false)
const rosters = ref<RosterOut[]>([])

const form = reactive({
  name: '',
  status: 'draft',
  note: '',
  defaultRosterId: null as number | null,
  enrollmentMode: 'per_player_code',
})

const STATUSES: Array<{ value: string; label: string }> = [
  { value: 'running', label: '进行中（正常收卷与下发）' },
  { value: 'draft', label: '筹备中（先建好，暂不使用）' },
  { value: 'frozen', label: '已封榜（停止下发，仍收卷）' },
  { value: 'closed', label: '已结束（不再自动扫成绩）' },
]

watch(visible, async (open) => {
  if (!open || !props.contest) return
  form.name = props.contest.name
  form.status = props.contest.status
  form.note = ''
  form.defaultRosterId = props.contest.default_roster_id ?? null
  form.enrollmentMode = props.contest.enrollment_mode ?? 'per_player_code'
  try {
    rosters.value = await rosterApi.list()
  } catch {
    rosters.value = []
  }
})

async function submit(): Promise<void> {
  if (!props.contest) return
  if (!form.name.trim()) {
    ElMessage.warning('场次名称不能为空')
    return
  }

  submitting.value = true
  try {
    const saved = await contestApi.update(props.contest.id, {
      name: form.name.trim(),
      status: form.status,
      note: form.note.trim() || null,
      // None 分不清"没传"和"要清空"，所以清空走显式开关
      clear_default_roster: form.defaultRosterId === null,
      default_roster_id: form.defaultRosterId,
      enrollment_mode: form.enrollmentMode,
    })
    ElMessage.success('已保存')
    visible.value = false
    emit('saved', saved)
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <el-dialog v-model="visible" title="场次设置" width="560px" :close-on-click-modal="false">
    <el-alert
      v-if="contest?.player_count"
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom: 12px"
    >
      <template #title>这场已经有 {{ contest.player_count }} 名选手了</template>
      <template #default>
        改「默认名单」只是换一个预设，<strong>不会动已有选手</strong>；
        要真正落到场次得去「名单库」页点「应用」。
      </template>
    </el-alert>

    <el-form label-width="90px">
      <el-form-item label="名称">
        <el-input v-model="form.name" maxlength="200" />
      </el-form-item>

      <el-form-item label="标识">
        <el-input :model-value="contest?.slug ?? ''" disabled />
        <div class="page-hint">
          标识已经固定在磁盘路径（<code>source/&lt;标识&gt;/…</code>）与评测器配置里，
          改它会让已有成绩找不到位置，所以这里只读。
        </div>
      </el-form-item>

      <el-form-item label="状态">
        <el-select v-model="form.status" style="width: 100%">
          <el-option v-for="item in STATUSES" :key="item.value" :label="item.label" :value="item.value" />
        </el-select>
      </el-form-item>

      <el-form-item label="默认名单">
        <el-select
          v-model="form.defaultRosterId"
          placeholder="不预设名单"
          clearable
          style="width: 100%"
        >
          <el-option
            v-for="roster in rosters"
            :key="roster.id"
            :label="`${roster.name}（${roster.entry_count} 人）`"
            :value="roster.id"
          />
        </el-select>
      </el-form-item>

      <el-form-item label="注册方式">
        <el-radio-group v-model="form.enrollmentMode">
          <el-radio value="per_player_code">每人一个注册码</el-radio>
          <el-radio value="bootstrap">镜像统一密钥 + 短码配对</el-radio>
        </el-radio-group>
      </el-form-item>

      <el-form-item label="备注">
        <el-input v-model="form.note" type="textarea" :rows="2" maxlength="500" />
      </el-form-item>
    </el-form>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="submit">保存</el-button>
    </template>
  </el-dialog>
</template>

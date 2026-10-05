<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'

import { contestApi, rosterApi } from '@/api'
import type { ContestOut, ContestUpdate, RosterOut } from '@/api/types'
import FormDialog from '@/components/FormDialog.vue'
import { useMutation } from '@/composables/useMutation'

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

const rosters = ref<RosterOut[]>([])

const STATUSES: Array<{ value: string; label: string }> = [
  { value: 'running', label: '进行中（正常收卷与下发）' },
  { value: 'draft', label: '筹备中（先建好，暂不使用）' },
  { value: 'frozen', label: '已封榜（停止下发，仍收卷）' },
  { value: 'closed', label: '已结束（不再自动扫成绩）' },
]

const form = reactive({
  name: '',
  status: 'draft',
  defaultRosterId: null as number | null,
})

watch(visible, async (open) => {
  if (!open || !props.contest) return
  form.name = props.contest.name
  form.status = props.contest.status
  form.defaultRosterId = props.contest.default_roster_id ?? null
  try {
    const page = await rosterApi.list({ limit: 500 })
    rosters.value = page.items
  } catch {
    // 拉不到名单不拦着改状态：名称、状态都不依赖它
    rosters.value = []
  }
})

const saveContest = useMutation(
  (payload: ContestUpdate) => {
    const contest = props.contest
    if (!contest) throw new Error('没有选中场次')
    return contestApi.update(contest.id, payload)
  },
  {
    success: '已保存',
    onDone: (saved) => {
      visible.value = false
      emit('saved', saved)
    },
  },
)

async function submit(): Promise<void> {
  await saveContest.run({
    name: form.name.trim(),
    status: form.status,
    // null 分不清"没传"和"要清空"，所以清空走显式开关
    clear_default_roster: form.defaultRosterId === null,
    default_roster_id: form.defaultRosterId,
  })
}
</script>

<template>
  <FormDialog
    v-model="visible"
    title="场次设置"
    width="560px"
    :submitting="saveContest.pending.value"
    :disabled="!form.name.trim()"
    @submit="submit"
  >
    <el-alert
      v-if="contest?.player_count"
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom: 12px"
    >
      <template #title>这场已经有 {{ contest.player_count }} 名选手了</template>
      <template #default>
        改「默认名单」只换预设，<strong>不动已有选手</strong>。
        要落到场次请到「名单库」点「应用」。
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
          要删掉整场请回列表页用「删除」——那个入口要求把标识原样打一遍。
        </div>
      </el-form-item>

      <el-form-item label="状态">
        <el-select v-model="form.status" style="width: 100%">
          <el-option
            v-for="item in STATUSES"
            :key="item.value"
            :label="item.label"
            :value="item.value"
          />
        </el-select>
        <div class="page-hint">
          「已封榜」停止下发新文件但仍然收卷 —— 封榜不影响已经在考的机器。
        </div>
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

      <!--
        备注这里**没有**编辑框：服务端的 `ContestOut` 不回传 `note`，一个空白的
        输入框在保存时没法区分"教师想清空"和"界面没读到"。等回执带上 note 再加，
        不能靠"先清空再让教师重打一遍"糊过去。
      -->
    </el-form>
  </FormDialog>
</template>
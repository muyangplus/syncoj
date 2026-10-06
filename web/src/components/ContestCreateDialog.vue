<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import type { FormInstance, FormRules } from 'element-plus'

import { contestApi, rosterApi } from '@/api'
import type { ContestCreate, ContestOut, RosterOut } from '@/api/types'
import FormDialog from '@/components/FormDialog.vue'
import { useMutation } from '@/composables/useMutation'

const props = defineProps<{
  modelValue: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  created: [contest: ContestOut]
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const formRef = ref<FormInstance>()
/** slug 是否由用户手工编辑过。没有的话跟着名称自动走。 */
const slugTouched = ref(false)

const rosters = ref<RosterOut[]>([])

/**
 * 名单列表在打开对话框时才拉 —— 名单库是全局的，和场次无关，
 * 而这里只需要一个下拉选项，没必要进主视图的数据流。
 *
 * 走分页信封，`limit` 给宽一点：名单是天然很小的集合（几十份），
 * 这里也确实需要全部选项。
 */
async function loadRosters(): Promise<void> {
  try {
    const page = await rosterApi.list({ limit: 500 })
    rosters.value = page.items
  } catch {
    // 拉不到就当成"没有名单"，不拦住建场次 —— 名单本来就是可选项
    rosters.value = []
  }
}

const form = reactive({
  name: '',
  slug: '',
  status: 'running',
  note: '',
  /**
   * 考试时间窗。类型是 `Date | null`，提交时才转成 ISO 字符串：
   * el-date-picker 给的是**本机时区**的那一刻，`.toISOString()` 把它变成
   * 带 Z 的 UTC，服务端按同一个时刻存下来 —— 页面上再显示出来还是这个点。
   * 直接提交本地格式的字符串会让"服务端按 UTC 解释"和"教师看着钟填的"差 8 小时。
   */
  startsAt: null as Date | null,
  endsAt: null as Date | null,
  defaultRosterId: null as number | null,
})

const rules: FormRules = {
  name: [{ required: true, message: '请输入场次名称', trigger: 'blur' }],
}

/**
 * 从名称生成 slug。
 *
 * 与服务端的 slugify 规则保持一致（只留 `[A-Za-z0-9._-]`，其余换成连字符）。
 * 这里只是为了给个顺眼的默认值；**服务端仍会独立校验并回退**，
 * 前端算错也不会出问题。
 */
function slugify(text: string): string {
  const cleaned = text
    .normalize('NFKD')
    .replace(/[^A-Za-z0-9._-]+/g, '-')
    .replace(/\.{2,}/g, '-')
    .replace(/-{2,}/g, '-')
    .replace(/^[-._]+|[-._]+$/g, '')
  return cleaned.slice(0, 64)
}

watch(
  () => form.name,
  (name) => {
    if (!slugTouched.value) form.slug = slugify(name)
  },
)

watch(visible, (open) => {
  if (!open) return
  form.name = ''
  form.slug = ''
  form.status = 'running'
  form.note = ''
  form.startsAt = null
  form.endsAt = null
  form.defaultRosterId = null
  slugTouched.value = false
  formRef.value?.clearValidate()
  void loadRosters()
})

const createContest = useMutation(
  (payload: ContestCreate) => contestApi.create(payload),
  {
    // 服务端的回执只有实体本身，这里自己说一句更清楚是哪一个
    success: (contest) => `场次「${contest.name}」已创建`,
    onDone: (contest) => {
      visible.value = false
      emit('created', contest)
    },
  },
)

async function submit(): Promise<void> {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  await createContest.run({
    name: form.name.trim(),
    // 空串要转成 null：服务端收到空串会当成"没给"，转而从名称生成
    slug: form.slug.trim() || null,
    status: form.status,
    note: form.note.trim() || null,
    // `null` = 这一端不限制（服务端的默认语义），不是"某个时刻"
    starts_at: form.startsAt ? form.startsAt.toISOString() : null,
    ends_at: form.endsAt ? form.endsAt.toISOString() : null,
    default_roster_id: form.defaultRosterId,
  })
}
</script>

<template>
  <FormDialog
    v-model="visible"
    title="新建场次"
    :submitting="createContest.pending.value"
    :disabled="!form.name.trim()"
    confirm-text="创建"
    @submit="submit"
  >
    <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
      <el-form-item label="名称" prop="name">
        <el-input v-model="form.name" placeholder="例如 2025 校内模拟赛" maxlength="200" />
      </el-form-item>

      <el-form-item label="标识">
        <el-input
          v-model="form.slug"
          placeholder="留空则从名称自动生成"
          @input="slugTouched = true"
        />
        <div class="page-hint">
          用于服务端目录名（<code>source/&lt;标识&gt;/…</code>）。
          <strong>创建后不建议改动。</strong>
        </div>
      </el-form-item>

      <el-form-item label="状态">
        <el-select v-model="form.status" style="width: 100%">
          <el-option label="进行中（正常收卷与下发）" value="running" />
          <el-option label="筹备中（先建好，暂不使用）" value="draft" />
          <el-option label="已封榜（停止下发，仍收卷）" value="frozen" />
          <el-option label="已结束（不再自动扫成绩）" value="closed" />
        </el-select>
      </el-form-item>

      <el-form-item label="开考时间">
        <el-date-picker
          v-model="form.startsAt"
          type="datetime"
          placeholder="留空 = 不限制"
          clearable
          style="width: 100%"
        />
      </el-form-item>

      <el-form-item label="结束时间">
        <el-date-picker
          v-model="form.endsAt"
          type="datetime"
          placeholder="留空 = 不限制"
          clearable
          style="width: 100%"
        />
        <div class="page-hint">
          <strong>留空 = 不限制</strong>，只填一个也合法。
          到结束时间之后机器不再接收代码（心跳与题面照常）。
        </div>
      </el-form-item>

      <el-form-item label="默认名单">
        <el-select
          v-model="form.defaultRosterId"
          placeholder="可选：留空则之后手动导入选手"
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
        <div class="page-hint">
          只是个预设：创建后到「名单库」点一次「应用」才会把选手落到这个场次。
          <span v-if="!rosters.length">还没有名单，可以先去「名单库」建一份。</span>
        </div>
      </el-form-item>

      <el-form-item label="备注">
        <el-input v-model="form.note" type="textarea" :rows="2" maxlength="500" />
      </el-form-item>
    </el-form>

    <el-alert type="info" :closable="false" show-icon>
      <template #title>建好还有两步</template>
      <template #default>
        ① 把选手落到这场（导入，或从「名单库」应用一份）
        ② 让机器注册上来（签发统一密钥）
      </template>
    </el-alert>
  </FormDialog>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'

import { contestApi, rosterApi } from '@/api'
import type { ContestOut, RosterOut } from '@/api/types'

const props = defineProps<{
  modelValue: boolean
  /** 用于生成默认 slug 的建议值（例如"校内模拟赛"→ mock） */
  existing?: ContestOut[]
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
const submitting = ref(false)
/** slug 是否由用户手工编辑过。没有的话跟着名称自动走。 */
const slugTouched = ref(false)

const rosters = ref<RosterOut[]>([])

/**
 * 名单列表在打开对话框时才拉 —— 名单库是全局的，和场次无关，
 * 而这里只需要一个下拉选项，没必要进主视图的数据流。
 */
async function loadRosters(): Promise<void> {
  try {
    rosters.value = await rosterApi.list()
  } catch {
    // 拉不到就当成"没有名单"，不拦住建场次 —— 名单本来就是可选项
    rosters.value = []
  }
}

const form = reactive({
  name: '',
  slug: '',
  status: 'draft',
  note: '',
  defaultRosterId: null as number | null,
  enrollmentMode: 'per_player_code',
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
  form.defaultRosterId = null
  form.enrollmentMode = 'per_player_code'
  slugTouched.value = false
  formRef.value?.clearValidate()
  void loadRosters()
})

async function submit(): Promise<void> {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  submitting.value = true
  try {
    const contest = await contestApi.create({
      name: form.name.trim(),
      // 空串要转成 null：服务端收到空串会当成"没给"，转而从名称生成
      slug: form.slug.trim() || null,
      status: form.status,
      note: form.note.trim() || null,
      default_roster_id: form.defaultRosterId,
      enrollment_mode: form.enrollmentMode,
    })
    ElMessage.success(`场次「${contest.name}」已创建`)
    visible.value = false
    emit('created', contest)
  } catch (error) {
    // 最常见的是 409（slug 已存在）。原样展示服务端提示，不要自己加工。
    ElMessage.error((error as Error).message)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <el-dialog v-model="visible" title="新建场次" width="520px" :close-on-click-modal="false">
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
          用于服务端目录名（<code>source/&lt;标识&gt;/…</code>）与结果目录。
          <strong>创建后不建议改动</strong> —— 它已经固定在磁盘路径与评测器配置里了。
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
          只是一个<strong>预设</strong> —— 创建后到「名单库」页点一次「应用」才会把
          选手落到这个场次。改名单不会自动影响场次。
          <span v-if="!rosters.length">
            还没有名单，可以先去「名单库」建一份。
          </span>
        </div>
      </el-form-item>

      <el-form-item label="注册方式">
        <el-radio-group v-model="form.enrollmentMode">
          <el-radio value="per_player_code">每人一个注册码</el-radio>
          <el-radio value="bootstrap">镜像统一密钥 + 短码配对</el-radio>
        </el-radio-group>
        <div class="page-hint">
          一个选手一个注册码适合临时加人、小规模现场发码；
          统一密钥适合整间机房用同一份镜像预装 —— 机器注册后没有归属，
          由教师在「机器配对」里认领到人。
        </div>
      </el-form-item>

      <el-form-item label="备注">
        <el-input v-model="form.note" type="textarea" :rows="2" maxlength="500" />
      </el-form-item>
    </el-form>

    <el-alert type="info" :closable="false" show-icon>
      <template #title>创建后还需要两步才能用</template>
      <template #default>
        ① 把选手落到场次（导入，或从「名单库」应用一份）
        ② 让考试机注册上来（签发注册码，或装好带统一密钥的镜像）
      </template>
    </el-alert>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="submit">创建</el-button>
    </template>
  </el-dialog>
</template>

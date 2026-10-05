<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'

import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const formRef = ref<FormInstance>()
const loading = ref(false)
const form = reactive({ username: 'admin', password: '' })

const rules: FormRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入口令', trigger: 'blur' }],
}

async function submit(): Promise<void> {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  loading.value = true
  try {
    await auth.login(form.username.trim(), form.password)
    ElMessage.success('登录成功')
    const redirect = route.query.redirect
    await router.replace(typeof redirect === 'string' ? redirect : { name: 'overview' })
  } catch (error) {
    // 服务端不区分"用户名不存在"与"口令错误"（避免枚举用户名），
    // 所以这里原样展示服务端的提示，不要自己加工
    ElMessage.error((error as Error).message)
    form.password = ''
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  if (auth.isAuthenticated) void router.replace({ name: 'overview' })
})
</script>

<template>
  <div class="login-page">
    <el-card class="login-card" shadow="always">
      <div class="brand">
        <div class="brand-title">SyncOJ</div>
        <div class="brand-sub">在线同步评测 · 管理台</div>
      </div>

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        @submit.prevent="submit"
      >
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" size="large" placeholder="admin" />
        </el-form-item>
        <el-form-item label="口令" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            size="large"
            show-password
            placeholder="请输入口令"
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-button
          type="primary"
          size="large"
          style="width: 100%"
          :loading="loading"
          @click="submit"
        >
          登录
        </el-button>
      </el-form>

      <p class="hint">
        首次部署请先运行 <code>syncoj-server init</code> 创建管理员账号。
      </p>
    </el-card>
  </div>
</template>

<style scoped>
.login-page {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #1f2d3d 0%, #2c405a 100%);
}

.login-card {
  width: 360px;
  border-radius: 8px;
}

.brand {
  text-align: center;
  margin-bottom: 20px;
}

.brand-title {
  font-size: 26px;
  font-weight: 700;
  letter-spacing: 1px;
  color: #1f2d3d;
}

.brand-sub {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
}

.hint {
  margin: 16px 0 0;
  font-size: 12px;
  color: #909399;
  line-height: 1.6;
}

.hint code {
  background: #f5f7fa;
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 11px;
}
</style>

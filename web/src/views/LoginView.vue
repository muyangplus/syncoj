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
    await router.replace(typeof redirect === 'string' ? redirect : { name: 'dashboard' })
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
  if (auth.isAuthenticated) void router.replace({ name: 'dashboard' })
})
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <div class="brand">
        <span class="brand-mark">S</span>
        <div class="brand-title">SyncOJ</div>
        <div class="brand-sub">考场同步评测 · 管理台</div>
      </div>

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        class="login-form"
        @submit.prevent="submit"
      >
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="例如 admin" autocomplete="username">
            <template #prefix><el-icon><User /></el-icon></template>
          </el-input>
        </el-form-item>
        <el-form-item label="口令" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            autocomplete="current-password"
            @keyup.enter="submit"
          >
            <template #prefix><el-icon><Lock /></el-icon></template>
          </el-input>
        </el-form-item>
        <el-button type="primary" class="submit" :loading="loading" @click="submit">登录</el-button>
      </el-form>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px 16px;
  background:
    radial-gradient(60% 50% at 50% 0%, var(--sj-primary-soft) 0%, transparent 70%),
    var(--sj-bg);
}

.login-card {
  width: 100%;
  max-width: 380px;
  padding: 32px 32px 28px;
  background: var(--sj-surface);
  border: 1px solid var(--sj-border);
  border-radius: 16px;
  box-shadow: var(--sj-shadow-lg);
}

.brand {
  display: flex;
  flex-direction: column;
  align-items: center;
  margin-bottom: 24px;
}

/* 与 AppLayout 侧栏的品牌方块一致 */
.brand-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 48px;
  height: 48px;
  margin-bottom: 14px;
  border-radius: 13px;
  background: var(--sj-brand-gradient);
  color: var(--el-color-white);
  font-weight: 700;
  font-size: 22px;
  box-shadow: var(--sj-shadow-lg);
}

.brand-title {
  font-size: 22px;
  font-weight: 700;
  letter-spacing: -0.01em;
  color: var(--sj-text);
}

.brand-sub {
  margin-top: 2px;
  font-size: 12.5px;
  color: var(--sj-text-3);
}

.login-form :deep(.el-input__wrapper) {
  padding-top: 3px;
  padding-bottom: 3px;
}

.submit {
  width: 100%;
  height: 40px;
  margin-top: 6px;
}
</style>

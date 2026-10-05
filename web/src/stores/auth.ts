import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { authApi } from '@/api'
import { getToken, setToken } from '@/api/client'

/**
 * 登录状态。
 *
 * token 存 localStorage（管理台是内网单管理员场景，没有多标签页共享的复杂度）。
 * **不在前端做权限判断** —— 前端只负责"有没有 token"，真正的授权一律由服务端
 * 决定；前端藏起来的按钮不构成任何安全保证。
 */
export const useAuthStore = defineStore('auth', () => {
  const username = ref<string | null>(null)
  // 是否已经尝试过恢复会话。路由守卫要等它，否则刷新页面会被误判成未登录
  const resolved = ref(false)

  const isAuthenticated = computed(() => username.value !== null)

  async function login(user: string, password: string): Promise<void> {
    const result = await authApi.login(user, password)
    setToken(result.token)
    username.value = result.username
    resolved.value = true
  }

  async function logout(): Promise<void> {
    try {
      await authApi.logout()
    } catch {
      // 服务端会话可能已经失效，本地照样要清干净
    }
    setToken(null)
    username.value = null
  }

  /** 页面刷新后用已存的 token 确认身份是否仍然有效。 */
  async function restore(): Promise<void> {
    if (resolved.value) return
    if (!getToken()) {
      resolved.value = true
      return
    }
    try {
      const info = await authApi.me()
      username.value = info.username
    } catch {
      setToken(null)
      username.value = null
    } finally {
      resolved.value = true
    }
  }

  /** 被 401 踢下线时调用（token 已被 client 清掉）。 */
  function clear(): void {
    username.value = null
  }

  return { username, resolved, isAuthenticated, login, logout, restore, clear }
})

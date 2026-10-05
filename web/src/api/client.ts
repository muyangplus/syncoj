/**
 * HTTP 客户端。
 *
 * 只有一层薄封装，不引 axios —— 需求就是"带 Bearer 头、解析 JSON、把错误
 * 变成异常"，fetch 已经够用，少一个依赖少一处版本风险。
 */

const TOKEN_KEY = 'syncoj.token'

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly detail?: unknown,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

/** 401 时的回调。由 App 注册 —— 直接 import router 会造成循环依赖。 */
type UnauthorizedHandler = () => void
let onUnauthorized: UnauthorizedHandler | null = null

export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  onUnauthorized = handler
}

export function getToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_KEY)
  } catch {
    // 隐私模式下 localStorage 可能抛异常，降级成"未登录"而不是崩掉整个应用
    return null
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) {
      window.localStorage.setItem(TOKEN_KEY, token)
    } else {
      window.localStorage.removeItem(TOKEN_KEY)
    }
  } catch {
    /* 忽略 */
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  /** JSON 请求体 */
  body?: unknown
  /** multipart 请求体（与 body 互斥） */
  form?: FormData
  signal?: AbortSignal
}

/** 把服务端返回的 detail 转成人能读的一句话。 */
function describeDetail(detail: unknown, fallback: string): string {
  if (typeof detail === 'string' && detail) return detail
  if (detail && typeof detail === 'object') {
    const record = detail as Record<string, unknown>
    // 下发的 409 会返回 {reason, message, expected} 这种结构化 detail
    if (typeof record.message === 'string') return record.message
    if (Array.isArray(detail)) {
      // pydantic 的校验错误是数组
      const first = detail[0] as Record<string, unknown> | undefined
      if (first && typeof first.msg === 'string') return first.msg
    }
  }
  return fallback
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' }

  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`

  let body: BodyInit | undefined
  if (options.form) {
    // 不要手动设 Content-Type：浏览器要自己补 multipart 的 boundary
    body = options.form
  } else if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json; charset=utf-8'
    body = JSON.stringify(options.body)
  }

  let response: Response
  try {
    response = await fetch(path, {
      method: options.method ?? 'GET',
      headers,
      body,
      signal: options.signal,
    })
  } catch (error) {
    if ((error as Error).name === 'AbortError') throw error
    // 网络层失败（断网、服务端没起）—— 给一句人话，而不是 "Failed to fetch"
    throw new ApiError(0, '无法连接服务端，请检查网络或服务是否在运行')
  }

  if (response.status === 401) {
    setToken(null)
    onUnauthorized?.()
    throw new ApiError(401, '登录已失效，请重新登录')
  }

  if (response.status === 204) return undefined as T

  const text = await response.text()
  let payload: unknown = undefined
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = text
    }
  }

  if (!response.ok) {
    const detail = (payload as { detail?: unknown } | undefined)?.detail ?? payload
    throw new ApiError(
      response.status,
      describeDetail(detail, `请求失败（HTTP ${response.status}）`),
      detail,
    )
  }

  return payload as T
}

/** 构造查询串，自动略过 undefined / null / 空串。 */
export function query(params: Record<string, string | number | boolean | undefined | null>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

/** 下载文件（导出 CSV 之类）。走 fetch 是为了能带上 Authorization 头。 */
export async function download(path: string, filename: string): Promise<void> {
  const token = getToken()
  const headers: Record<string, string> = {}
  if (token) headers.Authorization = `Bearer ${token}`

  const response = await fetch(path, { headers })
  if (!response.ok) {
    throw new ApiError(response.status, `下载失败（HTTP ${response.status}）`)
  }
  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}

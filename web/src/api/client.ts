/**
 * HTTP 客户端。
 *
 * 只有一层薄封装，不引 axios —— 需求就是"带 Bearer 头、解析 JSON、把错误
 * 变成异常"，fetch 已经够用，少一个依赖少一处版本风险。
 *
 * 这里**唯一**的职责是把服务端的约定翻译成异常对象：`detail` 永远是一句能
 * 直接显示给人看的中文，`code` 是稳定的短标识符（要分支判断就用它，不要拿
 * `detail` 做字符串比较 —— 那句话随时会被改得更通顺）。见
 * `docs/api-conventions.md` §3。
 */

const TOKEN_KEY = 'syncoj.token'

/** 服务端的统一错误体。三个字段之外的都不认。 */
interface ErrorBody {
  detail?: unknown
  code?: unknown
  details?: unknown
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    /** 稳定的错误码。**永远有值** —— 服务端没给时兜底成 `error`。 */
    readonly code: string = 'error',
    /** 结构化补充信息（校验错误的 `errors` 列表、冲突对象 id…） */
    readonly details: Record<string, unknown> = {},
  ) {
    super(message)
    this.name = 'ApiError'
  }

  /** 服务端校验失败的逐字段明细。用来把红框标到具体输入框上。 */
  get fieldErrors(): { loc: string[]; message: string }[] {
    const errors = this.details?.errors
    if (!Array.isArray(errors)) return []
    return errors.map((item) => {
      const record = (item ?? {}) as Record<string, unknown>
      return {
        loc: Array.isArray(record.loc) ? record.loc.map(String) : [],
        message: String(record.message ?? ''),
      }
    })
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

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  return headers
}

/**
 * 网络层失败与 401 的统一处理。
 *
 * 断网时 `fetch` 抛的是 `TypeError: Failed to fetch` —— 那句话对教师毫无
 * 信息量，所以在这里换成人话。**AbortError 必须原样抛出**：它是调用方主动
 * 取消（切换筛选、离开页面），不是错误，吞掉它会让 `useList` 把过期请求
 * 当成失败显示出来。
 *
 * 错误体**一律当 JSON 解析**，二进制的下载接口也一样：服务端所有错误走的都是
 * 同一个错误体，所以"下载失败"也能拿到那句中文，而不是一个 HTTP 状态码。
 */
async function ensureOk(response: Response): Promise<void> {
  if (response.status === 401) {
    setToken(null)
    onUnauthorized?.()
    throw new ApiError(401, '登录已失效，请重新登录', 'unauthorized')
  }
  if (response.ok) return

  const body = await readJson(response)
  // `detail` 约定是字符串；万一撞上非字符串（老接口、代理改写），压成一句话
  // 而不是让 `[object Object]` 显示在提示条上
  const detail = body?.detail
  const message =
    typeof detail === 'string' && detail ? detail : `请求失败（HTTP ${response.status}）`
  const code = typeof body?.code === 'string' && body.code ? body.code : 'error'
  const details =
    body?.details && typeof body.details === 'object'
      ? (body.details as Record<string, unknown>)
      : {}
  throw new ApiError(response.status, message, code, details)
}

async function readJson(response: Response): Promise<ErrorBody | undefined> {
  const text = await response.text()
  if (!text) return undefined
  try {
    const parsed = JSON.parse(text)
    return parsed && typeof parsed === 'object' ? (parsed as ErrorBody) : undefined
  } catch {
    return undefined
  }
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers = authHeaders()
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
    throw new ApiError(0, '无法连接服务端，请检查网络或服务是否在运行', 'network_error')
  }

  if (response.status === 204) return undefined as T
  await ensureOk(response)

  const text = await response.text()
  if (!text) return undefined as T
  try {
    return JSON.parse(text) as T
  } catch {
    // 200 却给了非 JSON（比如被反向代理插了一页登录表单）—— 明确报出来，
    // 而不是让调用方拿到一个字符串然后在下游 `.items` 上崩掉
    throw new ApiError(response.status, '服务端返回了非 JSON 内容，请检查反向代理配置')
  }
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

/**
 * 取回二进制的原始响应。
 *
 * 走 `fetch` 而不是 `window.open` 是为了能带上 `Authorization` 头 —— 新标签页
 * 里没有这个头，服务端会回 401 并把一个 JSON 错误存成 .zip。
 */
async function fetchRaw(path: string): Promise<Response> {
  let response: Response
  try {
    response = await fetch(path, { headers: authHeaders() })
  } catch {
    throw new ApiError(0, '无法连接服务端，请检查网络或服务是否在运行', 'network_error')
  }
  await ensureOk(response)
  return response
}

/** 下载文件（导出 zip/CSV、取代码正文）。走 fetch 是为了能带上 Authorization 头。 */
export async function download(path: string, filename: string): Promise<void> {
  const response = await fetchRaw(path)
  const blob = await response.blob()
  saveBlob(blob, filename)
}

/** 把二进制响应当成文本读回来。代码正文查看器用它。 */
export async function fetchText(path: string): Promise<string> {
  const response = await fetchRaw(path)
  return response.text()
}

/** 导出/下载的响应可能带 `Content-Disposition`，优先用服务端给的文件名。 */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  // 立刻 revoke 会让某些浏览器来不及取到内容，下一轮事件循环再放
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

/** 从 `Content-Disposition` 里抠出文件名。扣不出来就返回 null。 */
export function filenameFromResponse(response: Response): string | null {
  const header = response.headers.get('Content-Disposition') ?? ''
  const utf8 = /filename\*=UTF-8''([^;]+)/i.exec(header)
  if (utf8) {
    try {
      return decodeURIComponent(utf8[1])
    } catch {
      /* 编码坏了就退回去找 ASCII 版本 */
    }
  }
  const ascii = /filename="([^"]+)"/i.exec(header)
  return ascii ? ascii[1] : null
}

/** 下载并采用服务端的文件名。 */
export async function downloadWithServerName(path: string, fallback: string): Promise<void> {
  const response = await fetchRaw(path)
  const blob = await response.blob()
  saveBlob(blob, filenameFromResponse(response) ?? fallback)
}

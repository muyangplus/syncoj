import { ref } from 'vue'
import { ElMessage } from 'element-plus'

import { describeError } from '@/api/crud'
import type { SimpleAck } from '@/api/types'

export interface UseMutationOptions<T, R> {
  /**
   * 成功提示。
   *
   * 默认取服务端 `SimpleAck.detail` —— 那句话是**服务端**写的，它知道这次
   * 操作到底动了什么（"已删除 3 名选手；保留 2 名有提交的：S001、S002"）。
   * 界面自己编一句"操作成功"会把这些信息全丢掉。
   */
  success?: string | ((result: R, payload: T) => string)
  /** 失败时的额外处理。默认只是弹一句红字。 */
  onError?: (error: unknown) => void
  /** 成功后的收尾（刷新列表、关弹窗…）。 */
  onDone?: (result: R) => void | Promise<void>
}

/**
 * 一次会改数据的调用。
 *
 * 覆盖三件每次都一样、又每次都容易漏的事：把按钮置成 loading（防止连点两次
 * 删掉两条）、把错误弹成一句人话、成功后刷新列表。视图里写
 * `try { await api.foo() } catch (e) { ElMessage.error((e as Error).message) }`
 * 的代价是 6 行 × 40 个动作 —— 而漏掉 `pending` 的那一个就会真的删两次。
 */
export function useMutation<T, R = SimpleAck>(
  action: (payload: T) => Promise<R>,
  options: UseMutationOptions<T, R> = {},
) {
  const pending = ref(false)

  async function run(payload: T): Promise<R | null> {
    if (pending.value) return null
    pending.value = true
    try {
      const result = await action(payload)
      const message =
        typeof options.success === 'function'
          ? options.success(result, payload)
          : (options.success ?? ackDetail(result) ?? '已完成')
      if (message) ElMessage.success(message)
      await options.onDone?.(result)
      return result
    } catch (error) {
      // 取消不是错误：它只可能来自调用方主动 abort
      if ((error as Error | null)?.name !== 'AbortError') {
        ElMessage.error(describeError(error))
        options.onError?.(error)
      }
      return null
    } finally {
      pending.value = false
    }
  }

  return { run, pending }
}

/** 从 `SimpleAck` 里取服务端写好的回执。非这个形状就返回 null。 */
function ackDetail(result: unknown): string | null {
  if (!result || typeof result !== 'object') return null
  const detail = (result as { detail?: unknown }).detail
  return typeof detail === 'string' && detail ? detail : null
}

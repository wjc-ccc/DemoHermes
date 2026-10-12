import type { FilterValue } from '@/types/tables'
import type { PerceptionTraceDetail, TraceModule, TraceSheet } from '@/types/traces'
import { request } from './client'

/**
 * 目的：读取已注册的 trace 模式。加模式只增加这一列表里的项。
 * 得到：{ ok, data: { modules }, error, status }
 */
export async function listTraceModules() {
  return request<{ modules: TraceModule[] }>('/api/debug/traces/modules')
}

/**
 * 目的：按模式读取 trace 表。每一列是一个字段，行里包含 id 和当轮对话。
 * 得到：{ ok, data: TraceSheet, error, status }
 */
export async function listTraces(
  module: string, limit: number, offset: number,
  filters: Record<string, FilterValue> = {},
) {
  return request<TraceSheet>('/api/debug/traces', {
    query: { module, limit, offset, filters: JSON.stringify(filters) },
  })
}

export function getPerceptionContext(turnId: string, iteration?: number) {
  return request<PerceptionTraceDetail>('/api/debug/memory/trace', {
    query: { turn_id: turnId, iteration },
  })
}

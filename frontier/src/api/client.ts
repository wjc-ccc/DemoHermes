import config from '@/config'
import type { ApiResult } from '@/types/api'

function buildUrl(path: string, query?: Record<string, string | number | undefined | null>): string {
  const base = config.apiBaseUrl || ''
  const url = new URL(path, base || 'http://local.invalid')
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v === undefined || v === null) continue
      url.searchParams.set(k, String(v))
    }
  }
  if (base) return url.toString()
  return `${url.pathname}${url.search}`
}

/**
 * 目的：统一 JSON 请求、拼接 query、归一错误，作为前端唯一 HTTP 出站缝。
 * 得到：{ ok, data, error, status }
 */
export async function request<T = unknown>(
  path: string,
  options: {
    method?: string
    body?: unknown
    query?: Record<string, string | number | undefined | null>
  } = {},
): Promise<ApiResult<T>> {
  const { method = 'GET', body, query } = options
  const url = buildUrl(path, query)
  try {
    const init: RequestInit = { method, headers: {} }
    if (body !== undefined) {
      ;(init.headers as Record<string, string>)['Content-Type'] = 'application/json'
      init.body = JSON.stringify(body)
    }
    const resp = await fetch(url, init)
    let data: T | null = null
    try {
      data = (await resp.json()) as T
    } catch {
      data = null
    }
    if (!resp.ok) {
      const payload = data as { error?: string; message?: string } | null
      const errMsg = payload?.error || payload?.message || `HTTP ${resp.status}`
      return { ok: false, data, error: String(errMsg), status: resp.status }
    }
    return { ok: true, data, error: null, status: resp.status }
  } catch (e) {
    const err = e as Error
    return {
      ok: false,
      data: null,
      error: err?.message || String(e),
      status: null,
    }
  }
}

export function resolveEventsUrl(): string {
  const base = config.apiBaseUrl || ''
  if (base) return `${base.replace(/\/$/, '')}/api/events`
  return '/api/events'
}

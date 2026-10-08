import { afterEach, describe, expect, it, vi } from 'vitest'
import { request } from '../src/api/client'

describe('api client request', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('returns ok data from successful JSON response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => ({ reply: 'hi' }),
      })),
    )
    const result = await request('/api/chat', {
      method: 'POST',
      body: { text: 'ping' },
    })
    expect(result).toEqual({
      ok: true,
      data: { reply: 'hi' },
      error: null,
      status: 200,
    })
  })

  it('normalizes network failure', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('Failed to fetch')
      }),
    )
    const result = await request('/api/status')
    expect(result.ok).toBe(false)
    expect(result.data).toBeNull()
    expect(result.error).toContain('Failed to fetch')
  })

  it('surfaces backend error field when HTTP ok but payload has error', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => ({ reply: '', error: 'boom' }),
      })),
    )
    const result = await request('/api/chat', { method: 'POST', body: {} })
    expect(result.ok).toBe(true)
    expect((result.data as { error?: string })?.error).toBe('boom')
  })
})

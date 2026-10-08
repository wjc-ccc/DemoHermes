import { describe, expect, it } from 'vitest'
import { mapEmit } from '../src/utils/emitMapper'

describe('mapEmit', () => {
  it('maps turn_start to title and user_text detail', () => {
    const view = mapEmit({
      type: 'turn_start',
      data: { user_text: 'hello' },
    })
    expect(view.title).toBe('开始一轮对话')
    expect(view.detail).toBe('hello')
    expect(view.icon).toBe('▶')
  })

  it('maps llm_request with iteration stats', () => {
    const view = mapEmit({
      type: 'llm_request',
      data: { iteration: 2, message_count: 5, total_chars: 120 },
    })
    expect(view.title).toBe('调用大模型')
    expect(view.detail).toBe('第 2 次迭代 · 上下文 5 条消息 / 120 字符')
  })

  it('maps tool_call_result failure as step-err', () => {
    const view = mapEmit({
      type: 'tool_call_result',
      data: { ok: false, error: 'boom' },
    })
    expect(view.cls).toBe('step-err')
    expect(view.detail).toBe('失败: boom')
  })

  it('falls back for unknown type', () => {
    const view = mapEmit({
      type: 'custom_signal',
      data: { x: 1 },
    })
    expect(view.title).toBe('custom_signal')
    expect(view.detail).toContain('"x": 1')
  })
})

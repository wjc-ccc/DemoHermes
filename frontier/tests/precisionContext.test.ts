import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import PrecisionContextDialog from '../src/components/PrecisionContextDialog.vue'
import { contextText } from '../src/utils/contextText'
import { mapEmit } from '../src/utils/emitMapper'
import type { ContextBuildSnapshot } from '../src/types/traces'

const text = 'HEAD-' + '完整正文'.repeat(3000) + '-TAIL'
const snapshot: ContextBuildSnapshot = {
  iteration: 2,
  messages: [{ role: 'system', content: '规则' }, { role: 'tool', tool_call_id: 'c1', content: text }],
  tools: [{ type: 'function', function: { name: 'read_file', description: '完整工具定义' } }],
  records: [], metrics: null, error: null,
}

describe('Precision complete context', () => {
  afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = '' })

  it('renders complete ordered context and tools without truncation', () => {
    const formatted = contextText(snapshot)
    expect(formatted.indexOf('[0] system')).toBeLessThan(formatted.indexOf('[1] tool'))
    expect(formatted).toContain(text)
    expect(formatted).toContain('tool_call_id: "c1"')
    expect(formatted).toContain('完整工具定义')
  })

  it('keeps turn and iteration for the Loop context entry', () => {
    const step = mapEmit({ type: 'llm_request', turn_id: 't1', data: { iteration: 2 } })
    expect(step.turnId).toBe('t1')
    expect(step.iteration).toBe(2)
  })

  it('loads the selected iteration and switches complete text to raw JSON', async () => {
    const fetchMock = vi.fn(async (_input: unknown) => ({ ok: true, status: 200, json: async () => ({
      turn_id: 't1', trace: null, window: null, iterations: [{ iteration: 2, available: true, error: null }],
      build: snapshot, unavailable_reason: null,
    }) }))
    vi.stubGlobal('fetch', fetchMock)
    const root = document.createElement('div')
    document.body.append(root)
    const app = createApp(PrecisionContextDialog, { turnId: 't1', iteration: 2 })
    app.mount(root)
    await new Promise(resolve => setTimeout(resolve, 0))
    await nextTick()
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain('iteration=2')
    expect(root.querySelector('.context-content')?.textContent).toContain(text)
    const jsonButton = Array.from(root.querySelectorAll('button')).find(button => button.textContent === '原始 JSON')!
    jsonButton.click()
    await nextTick()
    expect(JSON.parse(root.querySelector('.context-content')!.textContent!).messages[1].content).toBe(text)
    app.unmount()
  })
})

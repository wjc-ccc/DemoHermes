import type { EmitStepView, LoopEvent } from '@/types'

interface EmitMeta {
  icon: string
  cls: string
  title: string
}

const EMIT_TEMPLATES: Record<string, EmitMeta> = {
  turn_start: { icon: '▶', cls: '', title: '开始一轮对话' },
  llm_request: { icon: '↑', cls: '', title: '调用大模型' },
  llm_response: { icon: '↓', cls: '', title: '模型返回' },
  tool_call_start: { icon: '🔧', cls: '', title: '调用工具' },
  tool_call_result: { icon: '✔', cls: 'step-ok', title: '工具结果' },
  turn_end: { icon: '🏁', cls: 'step-ok', title: '本轮完成' },
  turn_error: { icon: '⚠', cls: 'step-err', title: '本轮出错' },
  outbound_reply: { icon: '📤', cls: '', title: '回复已投递' },
}

function formatDetail(type: string, data: Record<string, unknown>): string {
  const d = data || {}
  switch (type) {
    case 'turn_start':
      return String(d.user_text || '')
    case 'llm_request':
      return `第 ${d.iteration} 次迭代 · 上下文 ${d.message_count} 条消息 / ${d.total_chars} 字符`
    case 'llm_response': {
      const toolCalls = (d.tool_calls as Array<{ name: string; arguments: unknown }> | undefined) || []
      const tcs = toolCalls.map((t) => `→ ${t.name}(${JSON.stringify(t.arguments)})`)
      return [d.text_preview, ...tcs].filter(Boolean).join('\n')
    }
    case 'tool_call_start':
      return `${d.name}(${JSON.stringify(d.arguments)})`
    case 'tool_call_result':
      return d.ok
        ? `${d.result_preview || ''}  [${d.duration_ms}ms]`
        : `失败: ${d.error}`
    case 'turn_end':
      return `迭代 ${d.iterations} 次`
    case 'turn_error':
      return String(d.error || '')
    case 'outbound_reply':
      return String(d.text_preview || '')
    default:
      try {
        return JSON.stringify(d, null, 2)
      } catch {
        return String(d)
      }
  }
}

/**
 * 目的：把 LoopEvent 转成右栏可渲染的步骤视图。
 * 得到：{ type, icon, title, cls, detail }
 */
export function mapEmit(event: Partial<LoopEvent> | null | undefined): EmitStepView {
  const type = event?.type || 'unknown'
  const data = (event?.data || {}) as Record<string, unknown>
  const meta = EMIT_TEMPLATES[type] || {
    icon: '•',
    cls: '',
    title: type,
  }
  let cls = meta.cls
  if (data.ok === false) cls = 'step-err'
  return {
    type,
    icon: meta.icon,
    title: meta.title,
    cls,
    detail: formatDetail(type, data),
  }
}

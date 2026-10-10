import type { ContextBuildSnapshot } from '@/types/traces'

export function contextText(build: ContextBuildSnapshot): string {
  const sections = (build.messages || []).map((message, index) => {
    const parts = [`[${index}] ${String(message.role || 'unknown')}`]
    if (message.content !== null && message.content !== undefined) {
      parts.push(typeof message.content === 'string' ? message.content : JSON.stringify(message.content, null, 2))
    }
    for (const key of ['tool_call_id', 'tool_calls', '_images']) {
      if (message[key] !== undefined) parts.push(`${key}: ${JSON.stringify(message[key], null, 2)}`)
    }
    return parts.join('\n')
  })
  sections.push(`工具定义 (${build.tools.length})\n${JSON.stringify(build.tools, null, 2)}`)
  return sections.join('\n\n')
}

import type { LoopEvent } from '@/types/events'
import { resolveEventsUrl } from './client'

export interface EventStreamHandle {
  close: () => void
}

/**
 * 目的：订阅 Agent Loop 的 SSE 事件流，供右栏逐步渲染。
 * 得到：{ close() }；onEvent 每次收到一条已解析的 LoopEvent。
 */
export function openEventStream(options: {
  onEvent?: (event: LoopEvent) => void
  onError?: (err: unknown) => void
} = {}): EventStreamHandle {
  const { onEvent, onError } = options
  const es = new EventSource(resolveEventsUrl())
  es.onmessage = (e) => {
    try {
      const event = JSON.parse(e.data) as LoopEvent
      onEvent?.(event)
    } catch (err) {
      onError?.(err)
    }
  }
  es.onerror = (err) => {
    onError?.(err)
  }
  return {
    close() {
      es.close()
    },
  }
}

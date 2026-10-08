import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import config from '@/config'
import { openEventStream, type EventStreamHandle } from '@/api/events'
import { mapEmit } from '@/utils/emitMapper'
import type { LoopEvent, LoopTurnCard } from '@/types'

export const useLoopStore = defineStore('loop', () => {
  const turns = ref(new Map<string, LoopTurnCard>())
  const turnOrder = ref<string[]>([])
  let stream: EventStreamHandle | null = null

  const turnList = computed(() =>
    turnOrder.value.map((id) => turns.value.get(id)).filter(Boolean) as LoopTurnCard[],
  )

  function ingest(event: LoopEvent) {
    const turnId = event.turn_id || 'misc'
    if (!turns.value.has(turnId)) {
      const next = new Map(turns.value)
      next.set(turnId, {
        turnId,
        createdAt: event.created_at || Date.now() / 1000,
        open: true,
        steps: [],
      })
      turns.value = next
      turnOrder.value = [...turnOrder.value, turnId]
      while (turnOrder.value.length > config.loopMaxTurns) {
        const oldest = turnOrder.value.shift()
        if (!oldest) break
        const trimmed = new Map(turns.value)
        trimmed.delete(oldest)
        turns.value = trimmed
      }
    }
    const card = turns.value.get(turnId)
    if (!card) return
    card.steps.push({
      id: event.event_id || `${turnId}-${card.steps.length}`,
      ...mapEmit(event),
    })
    if (event.type === 'turn_end' || event.type === 'turn_error') {
      card.open = true
    }
    turns.value = new Map(turns.value)
  }

  function clear() {
    turns.value = new Map()
    turnOrder.value = []
  }

  function start() {
    stop()
    stream = openEventStream({ onEvent: ingest })
  }

  function stop() {
    if (stream) {
      stream.close()
      stream = null
    }
  }

  return {
    turnList,
    ingest,
    clear,
    start,
    stop,
  }
})

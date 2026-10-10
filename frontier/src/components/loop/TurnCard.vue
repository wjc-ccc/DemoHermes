<script setup lang="ts">
import { formatClock } from '@/utils/format'
import StepItem from './StepItem.vue'
import type { LoopTurnCard } from '@/types/events'

defineProps<{
  turn: LoopTurnCard
}>()
</script>

<template>
  <details class="turn-card" :open="turn.open">
    <summary>
      <span>轮次 {{ turn.turnId.slice(0, 6) }}</span>
      <span>{{ formatClock(turn.createdAt) }}</span>
    </summary>
    <div class="turn-steps">
      <StepItem v-for="s in turn.steps" :key="s.id" :step="s" />
    </div>
  </details>
</template>

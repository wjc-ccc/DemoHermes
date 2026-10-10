<script setup lang="ts">
import type { EmitStepView } from '@/types/events'
import { ref } from 'vue'
import PrecisionContextDialog from '@/components/PrecisionContextDialog.vue'

const contextOpen = ref(false)

defineProps<{
  step: EmitStepView & { id?: string }
}>()
</script>

<template>
  <div class="step" :class="step.cls">
    <div class="step-icon">{{ step.icon }}</div>
    <div class="step-body">
      <div class="step-title">{{ step.title }}</div>
      <div v-if="step.detail" class="step-detail">{{ step.detail }}</div>
      <button v-if="step.type === 'llm_request' && step.turnId" class="btn btn-ghost btn-sm" @click="contextOpen = true">查看完整上下文</button>
    </div>
  </div>
  <Teleport to="body">
    <PrecisionContextDialog v-if="contextOpen && step.turnId" :turn-id="step.turnId" :iteration="step.iteration" @close="contextOpen = false" />
  </Teleport>
</template>

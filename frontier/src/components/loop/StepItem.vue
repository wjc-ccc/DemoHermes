<script setup lang="ts">
import type { EmitStepView } from '@/types'

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
    </div>
  </div>
</template>

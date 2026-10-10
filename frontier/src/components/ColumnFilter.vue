<script setup lang="ts">
import type { FilterValue } from '@/types/tables'

const props = defineProps<{
  name: string
  options: FilterValue[]
  active: boolean
  value?: FilterValue
}>()
const emit = defineEmits<{ change: [value: FilterValue | undefined] }>()

function label(value: FilterValue) {
  if (value === null) return '空值'
  if (value === '') return '空字符串'
  return String(value)
}

function change(event: Event) {
  const token = (event.target as HTMLSelectElement).value
  emit('change', token === '' ? undefined : JSON.parse(token) as FilterValue)
}
</script>

<template>
  <select
    class="column-filter"
    :class="{ active }"
    :aria-label="`筛选 ${name}`"
    :title="`按 ${name} 筛选`"
    :value="active ? JSON.stringify(props.value) : ''"
    @change="change"
  >
    <option value="">{{ name }} · 全部</option>
    <option v-for="option in options" :key="JSON.stringify(option)" :value="JSON.stringify(option)">
      {{ name }} · {{ label(option) }}
    </option>
  </select>
</template>

<style scoped>
.column-filter {
  display: block; max-width: 220px; width: 100%; min-width: 132px;
  border: 1px solid transparent; border-radius: 5px; padding: 4px 20px 4px 5px;
  background: transparent; color: var(--text); font: inherit; font-weight: 600;
  cursor: pointer;
}
.column-filter:hover, .column-filter:focus-visible, .column-filter.active {
  border-color: var(--primary); color: var(--primary); background-color: var(--primary-soft);
}
</style>

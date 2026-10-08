<script setup lang="ts">
import { ref } from 'vue'

const props = defineProps<{
  disabled?: boolean
}>()
const emit = defineEmits<{
  send: [text: string]
}>()
const text = ref('')

function onSubmit() {
  const value = text.value.trim()
  if (!value || props.disabled) return
  emit('send', value)
  text.value = ''
}
</script>

<template>
  <form class="chat-input-bar" @submit.prevent="onSubmit">
    <input
      v-model="text"
      type="text"
      placeholder="输入消息，Enter 发送…"
      autocomplete="off"
      :disabled="disabled"
    />
    <button type="submit" class="btn btn-primary" :disabled="disabled">
      发送
    </button>
  </form>
</template>

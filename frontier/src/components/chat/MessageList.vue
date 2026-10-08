<script setup lang="ts">
import type { ChatMessage } from '@/types'

defineProps<{
  messages?: ChatMessage[]
}>()
</script>

<template>
  <div class="chat-list">
    <div v-if="!(messages && messages.length)" class="chat-empty">
      <p>你好，我是 DemoCursor Agent。</p>
      <p class="hint">
        试试：<code>帮我算一下 (123+456)*7</code> 或 <code>帮我写份周报</code>，右侧面板会实时展示我的思考与工具调用过程。
      </p>
    </div>
    <div
      v-for="m in messages || []"
      :key="m.id"
      class="msg"
      :class="`msg-${m.role}`"
    >
      {{ m.text }}
    </div>
  </div>
</template>

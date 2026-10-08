<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import MessageList from '@/components/chat/MessageList.vue'
import ChatInput from '@/components/chat/ChatInput.vue'
import ThinkingPlaceholder from '@/components/chat/ThinkingPlaceholder.vue'
import TurnCard from '@/components/loop/TurnCard.vue'
import { useChatStore } from '@/stores/chat'
import { useLoopStore } from '@/stores/loop'

const chat = useChatStore()
const loop = useLoopStore()
const chatListEl = ref<HTMLElement | null>(null)
const loopListEl = ref<HTMLElement | null>(null)

watch(
  () => [chat.messages.length, chat.sending] as const,
  async () => {
    await nextTick()
    if (chatListEl.value) chatListEl.value.scrollTop = chatListEl.value.scrollHeight
  },
)

watch(
  () => loop.turnList.map((t) => t.steps.length).join(','),
  async () => {
    await nextTick()
    if (loopListEl.value) loopListEl.value.scrollTop = loopListEl.value.scrollHeight
  },
)
</script>

<template>
  <main class="layout">
    <section class="chat-panel">
      <div ref="chatListEl" class="chat-scroll">
        <MessageList :messages="chat.messages" />
        <div class="thinking-slot">
          <ThinkingPlaceholder :show="chat.sending" />
        </div>
      </div>
      <ChatInput :disabled="chat.sending" @send="chat.submitUserText" />
    </section>

    <aside class="loop-panel">
      <div class="loop-header">
        <span>Agent Loop 可视化</span>
        <button class="btn btn-ghost btn-sm" @click="loop.clear()">清空</button>
      </div>
      <div ref="loopListEl" class="loop-list">
        <div v-if="!loop.turnList.length" class="loop-empty">等待第一轮对话…</div>
        <TurnCard v-for="t in loop.turnList" :key="t.turnId" :turn="t" />
      </div>
    </aside>
  </main>
</template>

<style scoped>
.chat-scroll {
  flex: 1;
  overflow-y: auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.chat-scroll :deep(.chat-list) {
  overflow: visible;
  flex: 0 0 auto;
}
.thinking-slot {
  padding: 0 18px 12px;
}
</style>

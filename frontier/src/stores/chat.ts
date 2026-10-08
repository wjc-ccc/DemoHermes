import { defineStore } from 'pinia'
import { ref } from 'vue'
import { resetSession, sendChat } from '@/api/chat'
import type { ChatMessage } from '@/types'

export const useChatStore = defineStore('chat', () => {
  const messages = ref<ChatMessage[]>([])
  const sending = ref(false)

  function addMessage(role: ChatMessage['role'], text: string) {
    messages.value.push({
      id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      role,
      text,
    })
  }

  async function submitUserText(text: string) {
    const trimmed = String(text || '').trim()
    if (!trimmed || sending.value) return
    addMessage('user', trimmed)
    sending.value = true
    try {
      const result = await sendChat({ text: trimmed })
      if (!result.ok) {
        addMessage(
          'error',
          `无法连接后端：${result.error}\n请先运行 python main.py`,
        )
        return
      }
      if (result.data?.error) {
        addMessage('error', `出错了：${result.data.error}`)
      } else {
        addMessage('ai', result.data?.reply || '（空回复）')
      }
    } finally {
      sending.value = false
    }
  }

  async function newSession() {
    const result = await resetSession()
    if (!result.ok) {
      addMessage('error', `重开会话失败：${result.error}`)
      return
    }
    addMessage('ai', '已新开会话，之前的上下文已清空。')
  }

  return {
    messages,
    sending,
    addMessage,
    submitUserText,
    newSession,
  }
})

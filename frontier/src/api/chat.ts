import config from '@/config'
import type { ChatReply } from '@/types'
import { request } from './client'

/**
 * 目的：向 Gateway 发送一句用户消息，走完整 Agent turn。
 * 得到：{ ok, data: { reply, error? }, error, status }
 */
export async function sendChat(options: {
  text: string
  chatId?: string
  userId?: string
} ) {
  const {
    text,
    chatId = config.chatId,
    userId = config.userId,
  } = options
  return request<ChatReply>('/api/chat', {
    method: 'POST',
    body: { text, chat_id: chatId, user_id: userId },
  })
}

/**
 * 目的：重开当前会话（等同 /new），清空上下文。
 * 得到：{ ok, data: { reply }, error, status }
 */
export async function resetSession(options: {
  chatId?: string
  userId?: string
} = {}) {
  const { chatId = config.chatId, userId = config.userId } = options
  return request<ChatReply>('/api/reset', {
    method: 'POST',
    body: { chat_id: chatId, user_id: userId },
  })
}

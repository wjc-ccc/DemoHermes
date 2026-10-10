/** chat 类型。 */

export interface ChatReply {
  reply?: string
  error?: string
}

export interface ChatMessage {
  id: string
  role: 'user' | 'ai' | 'error' | 'thinking'
  text: string
}

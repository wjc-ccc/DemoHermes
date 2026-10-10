/** 与后端 LoopEvent、表目录、trace 模块对齐的前端类型。 */

export interface LoopEvent {
  event_id?: string
  type: string
  turn_id?: string
  session_key?: string
  session_id?: string
  data?: Record<string, unknown>
  created_at?: number
}

export interface EmitStepView {
  type: string
  icon: string
  title: string
  cls: string
  detail: string
  turnId?: string
  iteration?: number
}

export interface LoopTurnCard {
  turnId: string
  createdAt: number
  open: boolean
  steps: Array<EmitStepView & { id: string }>
}

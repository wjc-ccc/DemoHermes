/** 与后端 LoopEvent、表目录、trace 模块对齐的前端类型。 */

export interface ApiResult<T = unknown> {
  ok: boolean
  data: T | null
  error: string | null
  status: number | null
}

export interface ChatReply {
  reply?: string
  error?: string
}

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
}

export interface TableColumn {
  description?: string
  name: string
  type: string
  pk: boolean
  notnull: boolean
}

export interface TableSummary {
  group?: string
  description?: string
  name: string
  columns: TableColumn[]
}

export interface DatabaseCatalog {
  name: string
  tables: TableSummary[]
}

export interface TableCatalog {
  databases: DatabaseCatalog[]
}

export interface TablePage {
  database: string
  table: string
  columns: TableColumn[]
  filter_options: Record<string, FilterValue[]>
  rows: Record<string, unknown>[]
  total: number
  limit: number
  offset: number
}

export type FilterValue = string | number | boolean | null

export interface TraceModule {
  module: string
  label: string
  persisted: boolean
  note: string | null
}

export interface TraceColumn {
  name: string
}

export interface TraceSheet {
  module: string
  persisted: boolean
  note: string | null
  columns: TraceColumn[]
  filter_options: Record<string, FilterValue[]>
  rows: Record<string, unknown>[]
  total: number
  limit: number
  offset: number
}

export interface ChatMessage {
  id: string
  role: 'user' | 'ai' | 'error' | 'thinking'
  text: string
}

export interface LoopTurnCard {
  turnId: string
  createdAt: number
  open: boolean
  steps: Array<EmitStepView & { id: string }>
}

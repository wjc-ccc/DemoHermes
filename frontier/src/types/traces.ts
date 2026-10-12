/** traces 类型。 */
import type { FilterValue } from './tables'

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

export interface ContextBuildSnapshot {
  iteration: number
  messages: Record<string, unknown>[] | null
  tools: Record<string, unknown>[]
  records: Record<string, unknown>[]
  metrics: Record<string, unknown> | null
  error: string | null
}

export interface PerceptionTraceDetail {
  turn_id: string | null
  trace: Record<string, unknown> | null
  window: Record<string, unknown> | null
  iterations: Array<{ iteration: number; available: boolean; error: string | null }>
  build: ContextBuildSnapshot | null
  unavailable_reason: string | null
}

export interface ContextBuildSnapshot {
  iteration: number
  messages: Record<string, unknown>[] | null
  tools: Record<string, unknown>[]
  records: Record<string, unknown>[]
  metrics: Record<string, unknown> | null
  error: string | null
}

export interface PerceptionTraceDetail {
  turn_id: string | null
  trace: Record<string, unknown> | null
  window: Record<string, unknown> | null
  iterations: Array<{ iteration: number; available: boolean; error: string | null }>
  build: ContextBuildSnapshot | null
  unavailable_reason: string | null
}

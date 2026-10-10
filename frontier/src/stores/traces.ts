import { defineStore } from 'pinia'
import { ref } from 'vue'
import { listTraceModules, listTraces } from '@/api/traces'
import config from '@/config'
import type { FilterValue } from '@/types/tables'
import type { TraceModule, TraceSheet } from '@/types/traces'

const DISABLED = 'memory debug disabled'

export const useTraceStore = defineStore('traces', () => {
  const loading = ref(false)
  const error = ref('')
  const disabled = ref(false)
  const modules = ref<TraceModule[]>([])
  const selectedModule = ref('')
  const note = ref<string | null>(null)
  const persisted = ref(true)
  const sheet = ref<TraceSheet | null>(null)
  const offset = ref(0)
  const filters = ref<Record<string, FilterValue>>({})
  let requestId = 0

  async function refresh() {
    loading.value = true
    error.value = ''
    disabled.value = false
    try {
      const result = await listTraceModules()
      if (!result.ok) {
        modules.value = []
        sheet.value = null
        note.value = null
        disabled.value = result.status === 404 || result.error === DISABLED
        error.value = disabled.value ? '' : result.error || '读取模式失败'
        return
      }
      modules.value = result.data?.modules || []
      if (!modules.value.some((item) => item.module === selectedModule.value)) {
        selectedModule.value = modules.value[0]?.module || ''
        offset.value = 0
        filters.value = {}
      }
      if (selectedModule.value) await loadSheet()
      else {
        sheet.value = null
        note.value = null
      }
    } finally {
      loading.value = false
    }
  }

  async function select(module: string) {
    if (module !== selectedModule.value) {
      offset.value = 0
      filters.value = {}
    }
    selectedModule.value = module
    await loadSheet()
  }

  async function loadSheet() {
    if (!selectedModule.value) return
    const currentRequest = ++requestId
    const result = await listTraces(selectedModule.value, config.debugPageSize, offset.value, filters.value)
    if (currentRequest !== requestId) return
    if (!result.ok) {
      sheet.value = null
      disabled.value = result.status === 404 || result.error === DISABLED
      error.value = disabled.value ? '' : result.error || '读取 trace 失败'
      return
    }
    sheet.value = result.data
    note.value = result.data?.note ?? null
    persisted.value = result.data?.persisted ?? true
  }

  async function setFilter(column: string, value: FilterValue | undefined) {
    const next = { ...filters.value }
    if (value === undefined) delete next[column]
    else next[column] = value
    filters.value = next
    offset.value = 0
    await loadSheet()
  }

  async function prev() {
    offset.value = Math.max(0, offset.value - config.debugPageSize)
    await loadSheet()
  }

  async function next() {
    if (!sheet.value) return
    if (offset.value + sheet.value.limit >= sheet.value.total) return
    offset.value += sheet.value.limit
    await loadSheet()
  }

  return {
    loading,
    error,
    disabled,
    modules,
    selectedModule,
    note,
    persisted,
    sheet,
    offset,
    filters,
    refresh,
    select,
    setFilter,
    prev,
    next,
  }
})

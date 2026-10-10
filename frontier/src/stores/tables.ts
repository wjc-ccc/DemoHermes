import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { getTableRows, listTables } from '@/api/tables'
import config from '@/config'
import type { DatabaseCatalog, FilterValue, TablePage, TableSummary } from '@/types/tables'

const DISABLED = 'memory debug disabled'

export const useTableStore = defineStore('tables', () => {
  const loading = ref(false)
  const error = ref('')
  const disabled = ref(false)
  const databases = ref<DatabaseCatalog[]>([])
  const selectedDatabase = ref('')
  const selectedTable = ref('')
  const page = ref<TablePage | null>(null)
  const offset = ref(0)
  const filters = ref<Record<string, FilterValue>>({})
  let requestId = 0

  const selectedColumns = computed(() => {
    const database = databases.value.find((item) => item.name === selectedDatabase.value)
    return database?.tables.find((table) => table.name === selectedTable.value)?.columns || []
  })

  async function refresh() {
    loading.value = true
    error.value = ''
    disabled.value = false
    try {
      const result = await listTables()
      if (!result.ok) {
        databases.value = []
        page.value = null
        disabled.value = result.status === 404 || result.error === DISABLED
        error.value = disabled.value ? '' : result.error || '读取表目录失败'
        return
      }
      databases.value = result.data?.databases || []
      const stillThere = databases.value.some(
        (database) =>
          database.name === selectedDatabase.value &&
          database.tables.some((table) => table.name === selectedTable.value),
      )
      if (!stillThere) {
        const first = databases.value.find((database) => database.tables.length)
        selectedDatabase.value = first?.name || ''
        selectedTable.value = first?.tables[0]?.name || ''
        offset.value = 0
        filters.value = {}
      }
      if (selectedDatabase.value && selectedTable.value) await loadPage()
      else page.value = null
    } finally {
      loading.value = false
    }
  }

  async function select(database: string, table: TableSummary) {
    if (database !== selectedDatabase.value || table.name !== selectedTable.value) filters.value = {}
    selectedDatabase.value = database
    selectedTable.value = table.name
    offset.value = 0
    await loadPage()
  }

  async function loadPage() {
    if (!selectedDatabase.value || !selectedTable.value) return
    const currentRequest = ++requestId
    const result = await getTableRows(
      selectedDatabase.value,
      selectedTable.value,
      config.debugPageSize,
      offset.value,
      filters.value,
    )
    if (currentRequest !== requestId) return
    if (!result.ok) {
      page.value = null
      disabled.value = result.status === 404 || result.error === DISABLED
      error.value = disabled.value ? '' : result.error || '读取表数据失败'
      return
    }
    page.value = result.data
  }

  async function setFilter(column: string, value: FilterValue | undefined) {
    const next = { ...filters.value }
    if (value === undefined) delete next[column]
    else next[column] = value
    filters.value = next
    offset.value = 0
    await loadPage()
  }

  async function prev() {
    offset.value = Math.max(0, offset.value - config.debugPageSize)
    await loadPage()
  }

  async function next() {
    if (!page.value) return
    if (offset.value + page.value.limit >= page.value.total) return
    offset.value += page.value.limit
    await loadPage()
  }

  return {
    loading,
    error,
    disabled,
    databases,
    selectedDatabase,
    selectedTable,
    page,
    offset,
    filters,
    selectedColumns,
    refresh,
    select,
    setFilter,
    prev,
    next,
  }
})

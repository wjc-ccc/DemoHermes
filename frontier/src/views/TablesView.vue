<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useTableStore } from '@/stores/tables'
import type { TableSummary } from '@/types/tables'
import { formatCell } from '@/utils/format'
import ColumnFilter from '@/components/ColumnFilter.vue'

interface TableEntry {
  database: string
  table: TableSummary
}

const tables = useTableStore()
const expanded = ref<Record<string, boolean>>({})
const openedKey = ref('')
const opened = ref<{ column: string; text: string } | null>(null)

const groups = computed(() => {
  const buckets = new Map<string, TableEntry[]>()
  for (const database of tables.databases) {
    for (const table of database.tables) {
      const id = table.group ?? '其他'
      const list = buckets.get(id) ?? []
      list.push({ database: database.name, table })
      buckets.set(id, list)
    }
  }
  return [...buckets.entries()].map(([id, entries]) => ({ id, label: id, tables: entries }))

})

onMounted(() => {
  if (!tables.databases.length && !tables.loading) void tables.refresh()
})

function cellText(column: string, value: unknown) {
  return formatCell(column, value)
}

watch(
  () => [tables.selectedDatabase, tables.selectedTable, tables.offset] as const,
    () => {
    closeCell()
  },
)

watch(
  groups,
  (next) => {
    if (Object.keys(expanded.value).length) return
    const current = tables.selectedTable ? groupOf(tables.selectedTable) : next[0]?.id
    if (current) expanded.value = { [current]: true }
  },
  { immediate: true },
)

function groupOf(name: string) {
  return tables.databases.flatMap((db) => db.tables).find((table) => table.name === name)?.group ?? '其他'
}

function toggleGroup(id: string) {
  expanded.value = { ...expanded.value, [id]: !expanded.value[id] }
}

function choose(database: string, table: TableSummary) {
  expanded.value = { ...expanded.value, [groupOf(table.name)]: true }
  void tables.select(database, table)
}

function isCurrent(database: string, table: TableSummary) {
  return tables.selectedDatabase === database && tables.selectedTable === table.name
}

function closeCell() {
  openedKey.value = ''
  opened.value = null
}

function openCell(rowIndex: number, column: string, value: unknown) {
  const text = cellText(column, value)
  const key = `${rowIndex}:${column}`
  if (!text || openedKey.value === key) {
    closeCell()
    return
  }
  openedKey.value = key
  opened.value = { column, text }
}
</script>

<template>
  <main class="observe-page">
    <div class="observe-toolbar">
      <button class="btn btn-primary" :disabled="tables.loading" @click="tables.refresh()">
        {{ tables.loading ? '刷新中…' : '刷新' }}
      </button>
    </div>
    <p v-if="tables.disabled" class="observe-note">后端未开启只读观察。启动时加上 --enable-memory-debug。</p>
    <p v-else-if="tables.error" class="msg msg-error">{{ tables.error }}</p>
    <div v-else class="observe-split">
      <aside class="observe-nav">
        <p v-if="!tables.databases.length" class="observe-empty">还没有已登记的数据库。</p>
        <section v-for="group in groups" :key="group.id" class="db-group">
          <button class="group-toggle" @click="toggleGroup(group.id)">
            <span class="chevron" :class="{ open: expanded[group.id] }">▸</span>
            {{ group.label }}
            <span class="group-count">{{ group.tables.length }}</span>
          </button>
          <div v-if="expanded[group.id]" class="group-tables">
            <button
              v-for="entry in group.tables"
              :key="`${entry.database}:${entry.table.name}`"
              class="table-option"
              :title="entry.table.description"
              :class="{ active: isCurrent(entry.database, entry.table) }"
              @click="choose(entry.database, entry.table)"
            >
              {{ entry.table.name }}
            </button>
          </div>
        </section>
      </aside>
      <section class="observe-main">
        <p v-if="!tables.page" class="observe-empty">选择一张表查看行。</p>
        <template v-else>
          <div class="observe-meta">
            {{ tables.page.database }} / {{ tables.page.table }} · {{ tables.page.total }} 行
          </div>
          <div class="grid-wrap">
            <table class="data-grid">
              <thead>
                <tr>
                  <th v-for="column in tables.page.columns" :key="column.name" :title="column.description">
                    <ColumnFilter
                      v-if="tables.page.filter_options?.[column.name]?.length"
                      :name="column.name"
                      :options="tables.page.filter_options[column.name]"
                      :active="Object.hasOwn(tables.filters, column.name)"
                      :value="tables.filters[column.name]"
                      @change="tables.setFilter(column.name, $event)"
                    />
                    <template v-else>{{ column.name }}</template>
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, index) in tables.page.rows" :key="index">
                  <td v-for="column in tables.page.columns" :key="column.name">
                    <button
                      class="cell"
                      :class="{ open: openedKey === `${index}:${column.name}` }"
                      :disabled="!cellText(column.name, row[column.name])"
                      @click="openCell(index, column.name, row[column.name])"
                    >
                      {{ cellText(column.name, row[column.name]) || '—' }}
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="pager">
            <button class="btn btn-ghost btn-sm" :disabled="tables.offset <= 0" @click="tables.prev()">
              上一页
            </button>
            <button
              class="btn btn-ghost btn-sm"
              :disabled="tables.offset + tables.page.limit >= tables.page.total"
              @click="tables.next()"
            >
              下一页
            </button>
          </div>
        </template>
      </section>
    </div>
    <div v-if="opened" class="modal-mask" @click="closeCell">
      <section class="modal" role="dialog" aria-modal="true" @click.stop>
        <header>
          <strong>{{ opened.column }}</strong>
          <button class="modal-close" type="button" aria-label="关闭" @click="closeCell">×</button>
        </header>
        <pre>{{ opened.text }}</pre>
      </section>
    </div>
  </main>
</template>

<style scoped>
.observe-page { flex: 1; min-height: 0; display: flex; flex-direction: column; gap: 12px; padding: 12px; }
.observe-toolbar { display: flex; gap: 8px; }
.observe-note, .observe-empty { color: var(--text-dim); line-height: 1.7; }
.observe-split { flex: 1; min-height: 0; display: grid; grid-template-columns: 240px minmax(0, 1fr); gap: 12px; }
.observe-nav, .observe-main {
  min-height: 0; background: var(--panel);
  border: 1px solid var(--border); border-radius: var(--radius); padding: 12px;
}
.observe-nav, .observe-main { overflow: auto; }
.db-group { display: flex; flex-direction: column; gap: 2px; margin-bottom: 8px; }
.group-toggle {
  display: flex; align-items: center; gap: 6px; width: 100%;
  border: 0; background: transparent; color: var(--text); cursor: pointer;
  font-weight: 600; font-size: 13px; padding: 6px 4px; border-radius: 6px; text-align: left;
}
.group-toggle:hover { background: var(--bg); }
.chevron { display: inline-block; width: 12px; color: var(--text-dim); transition: transform .15s; }
.chevron.open { transform: rotate(90deg); }
.group-count { margin-left: auto; color: var(--text-dim); font-weight: 400; font-size: 11px; }
.group-tables { display: flex; flex-direction: column; gap: 2px; padding-left: 16px; }
.table-option, th {
  text-align: left; border: 0; background: transparent;
  color: var(--text); border-radius: 6px; padding: 6px 8px; cursor: default;
}
.table-option { cursor: pointer; width: 100%; }
.table-option.active, .table-option:hover { background: var(--primary-soft); color: var(--primary); }
.observe-meta { font-size: 12px; color: var(--text-dim); margin-bottom: 8px; }
.grid-wrap { overflow: auto; }
.data-grid { border-collapse: collapse; width: max-content; min-width: 100%; font-size: 12px; }
.data-grid th, .data-grid td {
  border-bottom: 1px solid var(--border);
  border-right: 1px solid var(--border);
  padding: 0;
  vertical-align: top;
  min-width: 140px;
  max-width: 220px;
}
.data-grid th { padding: 8px; text-align: left; background: var(--bg); white-space: nowrap; }
.cell {
  display: block; width: 100%; max-width: 220px; padding: 8px;
  border: 0; background: transparent; color: inherit; text-align: left;
  font-family: var(--mono); font-size: 12px; cursor: pointer;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.cell:disabled { cursor: default; color: var(--text-dim); }
.cell.open, .cell:not(:disabled):hover { background: var(--primary-soft); color: var(--primary); }
.modal-mask {
  position: fixed; inset: 0; z-index: 20;
  background: rgba(15, 23, 42, 0.35);
  display: flex; align-items: center; justify-content: center; padding: 24px;
}
.modal {
  width: min(640px, 100%); max-height: min(70vh, 520px);
  display: flex; flex-direction: column;
  background: var(--panel); border-radius: 12px;
  box-shadow: 0 16px 48px rgba(15, 23, 42, 0.18);
}
.modal header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 12px 14px; border-bottom: 1px solid var(--border);
}
.modal-close {
  border: 0; background: transparent; color: var(--text-dim);
  font-size: 20px; line-height: 1; cursor: pointer; padding: 0 4px;
}
.modal-close:hover { color: var(--text); }
.modal pre {
  margin: 0; padding: 14px; overflow: auto;
  white-space: pre-wrap; word-break: break-word; font-family: var(--mono); font-size: 12px;
}
.pager { display: flex; gap: 8px; margin-top: 10px; }
@media (max-width: 800px) {
  .observe-split { grid-template-columns: 1fr; }
}
</style>

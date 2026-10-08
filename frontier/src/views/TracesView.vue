<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useTraceStore } from '@/stores/traces'
import { formatCell } from '@/utils/format'
import ColumnFilter from '@/components/ColumnFilter.vue'

const traces = useTraceStore()
const openedKey = ref('')
const opened = ref<{ column: string; text: string } | null>(null)

onMounted(() => {
  if (!traces.modules.length && !traces.loading) void traces.refresh()
})

watch(
  () => [traces.selectedModule, traces.offset] as const,
  () => closeCell(),
)

function cellText(column: string, value: unknown) {
  return formatCell(column, value)
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
      <button class="btn btn-primary" :disabled="traces.loading" @click="traces.refresh()">
        {{ traces.loading ? '刷新中…' : '刷新' }}
      </button>
    </div>
    <p v-if="traces.disabled" class="observe-note">后端未开启只读观察。启动时加上 --enable-memory-debug。</p>
    <p v-else-if="traces.error" class="msg msg-error">{{ traces.error }}</p>
    <div v-else class="observe-split">
      <aside class="observe-nav">
        <p v-if="!traces.modules.length" class="observe-empty">还没有已注册的模式。</p>
        <button
          v-for="item in traces.modules"
          :key="item.module"
          class="table-option"
          :class="{ active: traces.selectedModule === item.module }"
          @click="traces.select(item.module)"
        >
          {{ item.label }}
        </button>
      </aside>
      <section class="observe-main">
        <p v-if="!traces.sheet" class="observe-empty">选择一个模式查看 trace。</p>
        <template v-else>
          <p v-if="traces.note" class="observe-note">{{ traces.note }}</p>
          <p v-if="!traces.persisted" class="observe-note">这个模式的记录尚未落库。</p>
          <div class="observe-meta">
            {{ traces.sheet.module }} · {{ traces.sheet.total }} 行
          </div>
          <p v-if="!traces.sheet.rows.length" class="observe-empty">{{ Object.keys(traces.filters).length ? '没有符合筛选条件的 trace。' : '这个模式还没有 trace。' }}</p>
          <div class="grid-wrap">
            <table class="data-grid">
              <thead>
                <tr>
                  <th v-for="column in traces.sheet.columns" :key="column.name">
                    <ColumnFilter
                      v-if="traces.sheet.filter_options?.[column.name]?.length"
                      :name="column.name"
                      :options="traces.sheet.filter_options[column.name]"
                      :active="Object.hasOwn(traces.filters, column.name)"
                      :value="traces.filters[column.name]"
                      @change="traces.setFilter(column.name, $event)"
                    />
                    <template v-else>{{ column.name }}</template>
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, index) in traces.sheet.rows" :key="index">
                  <td v-for="column in traces.sheet.columns" :key="column.name">
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
            <button class="btn btn-ghost btn-sm" :disabled="traces.offset <= 0" @click="traces.prev()">
              上一页
            </button>
            <button
              class="btn btn-ghost btn-sm"
              :disabled="traces.offset + traces.sheet.limit >= traces.sheet.total"
              @click="traces.next()"
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
  min-height: 0; overflow: auto; background: var(--panel);
  border: 1px solid var(--border); border-radius: var(--radius); padding: 12px;
}
.table-option {
  display: block; width: 100%; text-align: left; border: 0; background: transparent;
  color: var(--text); border-radius: 6px; padding: 6px 8px; cursor: pointer;
}
.table-option.active, .table-option:hover { background: var(--primary-soft); color: var(--primary); }
.observe-meta { font-size: 12px; color: var(--text-dim); margin-bottom: 8px; }
.grid-wrap { overflow: auto; }
.data-grid { border-collapse: collapse; width: max-content; min-width: 100%; font-size: 12px; }
.data-grid th, .data-grid td {
  border-bottom: 1px solid var(--border);
  border-right: 1px solid var(--border);
  padding: 0; vertical-align: top; min-width: 140px; max-width: 220px;
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
.pager { display: flex; gap: 8px; margin-top: 10px; }
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
.modal pre {
  margin: 0; padding: 14px; overflow: auto;
  white-space: pre-wrap; word-break: break-word; font-family: var(--mono); font-size: 12px;
}
@media (max-width: 800px) {
  .observe-split { grid-template-columns: 1fr; }
}
</style>

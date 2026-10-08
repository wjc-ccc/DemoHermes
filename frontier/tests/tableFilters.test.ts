import { createApp, nextTick } from 'vue'
import { createPinia } from 'pinia'
import { afterEach, expect, test, vi } from 'vitest'
import TablesView from '../src/views/TablesView.vue'
import { getTableRows, listTables } from '../src/api/tables'
import config from '../src/config'

vi.mock('../src/api/tables', () => ({ listTables: vi.fn(), getTableRows: vi.fn() }))
afterEach(() => { document.body.innerHTML = ''; vi.clearAllMocks() })

test('column choice filters the whole table and leaves time columns plain', async () => {
  const columns = [
    { name: 'category', type: 'TEXT', pk: false, notnull: false },
    { name: 'created_at', type: 'INTEGER', pk: false, notnull: true },
  ]
  vi.mocked(listTables).mockResolvedValue({ ok: true, status: 200, error: null, data: {
    databases: [{ name: 'agent', tables: [{ name: 'items', columns }] }],
  } })
  vi.mocked(getTableRows).mockImplementation(async (_database, _table, limit, offset, filters) => ({
    ok: true, status: 200, error: null,
    data: {
      database: 'agent', table: 'items', columns, filter_options: { category: ['a', 'b'] },
      rows: filters?.category === 'a' ? [{ category: 'a', created_at: 1 }] : [
        { category: 'a', created_at: 1 }, { category: 'b', created_at: 2 },
      ],
      total: filters?.category === 'a' ? 1 : 2, limit, offset,
    },
  }))

  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp(TablesView)
  app.use(createPinia())
  app.mount(host)
  await vi.waitFor(() => expect(host.querySelector('select[aria-label="筛选 category"]')).not.toBeNull())
  expect(host.querySelector('select[aria-label="筛选 created_at"]')).toBeNull()

  const select = host.querySelector<HTMLSelectElement>('select[aria-label="筛选 category"]')!
  select.value = '"a"'
  select.dispatchEvent(new Event('change', { bubbles: true }))
  await vi.waitFor(() => expect(getTableRows).toHaveBeenLastCalledWith(
    'agent', 'items', config.debugPageSize, 0, { category: 'a' },
  ))
  await nextTick()
  expect(host.querySelector('.observe-meta')?.textContent).toContain('1 行')
  expect(host.querySelectorAll('tbody tr')).toHaveLength(1)
  app.unmount()
})

import type { FilterValue, TableCatalog, TablePage } from '@/types'
import { request } from './client'

/**
 * 目的：读取已登记 SQLite 的库、表和字段。
 * 得到：{ ok, data: TableCatalog, error, status }
 */
export async function listTables() {
  return request<TableCatalog>('/api/debug/tables')
}

/**
 * 目的：分页读取一张表的行。
 * 得到：{ ok, data: TablePage, error, status }
 */
export async function getTableRows(
  database: string, table: string, limit: number, offset: number,
  filters: Record<string, FilterValue> = {},
) {
  return request<TablePage>('/api/debug/tables/rows', {
    query: { database, table, limit, offset, filters: JSON.stringify(filters) },
  })
}

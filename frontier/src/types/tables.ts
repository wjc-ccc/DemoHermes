/** tables 类型。 */

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

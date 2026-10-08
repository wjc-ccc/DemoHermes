export function escapeHtml(s: unknown): string {
  return String(s ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
}

export function formatClock(tsSeconds?: number): string {
  const ms = (tsSeconds || Date.now() / 1000) * 1000
  return new Date(ms).toLocaleTimeString('zh-CN', { hour12: false })
}

function isTimeColumn(column: string) {
  return column === 'at' || column.endsWith('_at')
}

function pad(value: number) {
  return String(value).padStart(2, '0')
}

/** 时间列显示为本地年月日时分秒。数字按 Unix 秒或毫秒解析，文本按日期解析。 */
export function formatCell(column: string, value: unknown): string {
  if (value === null || value === undefined) return ''
  if (isTimeColumn(column)) {
    const formatted = formatTimestamp(value)
    if (formatted) return formatted
  }
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function formatTimestamp(value: unknown): string | null {
  const date = timestampDate(value)
  if (!date) return null
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

function timestampDate(value: unknown): Date | null {
  if (typeof value === 'number' && Number.isFinite(value)) return dateFromNumber(value)
  if (typeof value === 'string') {
    const trimmed = value.trim()
    if (!trimmed) return null
    if (/^-?\d+(\.\d+)?$/.test(trimmed)) return dateFromNumber(Number(trimmed))
    const parsed = new Date(trimmed)
    if (!Number.isNaN(parsed.getTime())) return parsed
  }
  return null
}

function dateFromNumber(value: number): Date | null {
  const abs = Math.abs(value)
  const ms = abs >= 1e12 ? value : value * 1000
  const date = new Date(ms)
  if (Number.isNaN(date.getTime())) return null
  const year = date.getFullYear()
  if (year < 1970 || year > 2100) return null
  return date
}

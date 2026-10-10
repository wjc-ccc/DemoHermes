/** api 类型。 */

export interface ApiResult<T = unknown> {
  ok: boolean
  data: T | null
  error: string | null
  status: number | null
}

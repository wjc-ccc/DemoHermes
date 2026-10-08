/**
 * 前端运行时配置 —— 端口、身份、分页等只写这里。
 * View / store / 组件禁止硬编码；vite.config.ts 的 proxy 也从此文件读取。
 */
export interface AppConfig {
  apiBaseUrl: string
  backendHost: string
  backendPort: number
  chatId: string
  userId: string
  loopMaxTurns: number
  frontendPort: number
  debugPageSize: number
}

const config: AppConfig = {
  apiBaseUrl: '',
  backendHost: '127.0.0.1',
  backendPort: 8000,
  chatId: 'web',
  userId: 'web-user',
  loopMaxTurns: 30,
  frontendPort: 5173,
  debugPageSize: 20,
}

export default config

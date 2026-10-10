/// <reference types="vitest/config" />
import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'
import config from './src/config'

export default defineConfig(({ mode }) => {
  // 后端 token 与 agent 共用项目根 .env；由开发代理在服务端注入，浏览器拿不到
  const { AGENT_HTTP_TOKEN: token } = loadEnv(
    mode,
    fileURLToPath(new URL('..', import.meta.url)),
    'AGENT_HTTP_',
  )
  return {
    plugins: [vue()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      port: config.frontendPort,
      proxy: {
        '/api': {
          target: `http://${config.backendHost}:${config.backendPort}`,
          changeOrigin: true,
          headers: token ? { Authorization: `Bearer ${token}` } : undefined,
        },
      },
    },
    test: {
      environment: 'jsdom',
      include: ['tests/**/*.test.ts'],
    },
  }
})

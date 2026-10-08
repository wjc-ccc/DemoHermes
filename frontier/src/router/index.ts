import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import ChatView from '@/views/ChatView.vue'

const routes: RouteRecordRaw[] = [
  { path: '/', name: 'chat', component: ChatView },
  { path: '/tables', name: 'tables', component: () => import('@/views/TablesView.vue') },
  { path: '/traces', name: 'traces', component: () => import('@/views/TracesView.vue') },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router

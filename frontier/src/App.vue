<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue'
import AppHeader from '@/components/layout/AppHeader.vue'
import { useLoopStore } from '@/stores/loop'

const loop = useLoopStore()

onMounted(() => {
  loop.start()
})

onUnmounted(() => {
  loop.stop()
})
</script>

<template>
  <div class="app-root">
    <AppHeader />
    <router-view />
  </div>
</template>

<style scoped>
.app-root {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
</style>

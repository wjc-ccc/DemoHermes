<script setup lang="ts">
import { RouterLink } from 'vue-router'
import { useChatStore } from '@/stores/chat'

const chat = useChatStore()
</script>

<template>
  <header class="topbar">
    <div class="topbar-right">
      <RouterLink class="btn btn-ghost" to="/tables" active-class="btn-active">数据库表</RouterLink>
      <RouterLink class="btn btn-ghost" to="/traces" active-class="btn-active">模式指标</RouterLink>
      <button class="btn btn-ghost" title="重开当前会话" @click="chat.newSession()">新会话</button>
    </div>
  </header>
</template>

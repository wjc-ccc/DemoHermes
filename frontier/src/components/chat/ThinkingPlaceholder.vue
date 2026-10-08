<script setup lang="ts">
defineProps<{
  show?: boolean
}>()
</script>

<template>
  <div v-if="show" class="msg msg-thinking">
    思考中…（右侧可观察执行步骤）
  </div>
</template>

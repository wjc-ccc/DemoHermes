<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { getPrecisionContext } from '@/api/traces'
import type { PrecisionTraceDetail } from '@/types/traces'
import { contextText } from '@/utils/contextText'

const props = defineProps<{ turnId: string; iteration?: number }>()
const emit = defineEmits<{ close: [] }>()
const detail = ref<PrecisionTraceDetail | null>(null)
const selected = ref(props.iteration)
const view = ref<'text' | 'json'>('text')
const loading = ref(false)
const error = ref('')
const copied = ref(false)
let generation = 0
const build = computed(() => detail.value?.build)
const fullText = computed(() => build.value ? (view.value === 'json'
  ? JSON.stringify({ messages: build.value.messages, tools: build.value.tools }, null, 2)
  : contextText(build.value)) : '')
const unavailable = computed(() => {
  switch (detail.value?.unavailable_reason) {
    case 'context_build_failed': return `该迭代构建失败，没有可调用的完整上下文：${build.value?.error || ''}`
    case 'iteration_not_found': return '没有找到该迭代的快照。'
    case 'full_context_not_saved': return '这条记录未保存全文，无法从预览还原。'
    default: return ''
  }
})

async function load(iteration?: number) {
  const requestGeneration = ++generation
  loading.value = true
  error.value = ''
  copied.value = false
  const result = await getPrecisionContext(props.turnId, iteration)
  if (requestGeneration !== generation) return
  loading.value = false
  if (!result.ok || !result.data) {
    error.value = result.status === 404 ? '只读观察未开启或记录不存在。' : result.error || '读取上下文失败'
    detail.value = null
    return
  }
  detail.value = result.data
  selected.value = result.data.build?.iteration ?? iteration
}

async function copyFullText() {
  try {
    await navigator.clipboard.writeText(fullText.value)
    copied.value = true
  } catch {
    error.value = '复制失败，请在全文区域选择并复制。'
  }
}

watch(() => [props.turnId, props.iteration] as const, () => {
  selected.value = props.iteration
  void load(props.iteration)
}, { immediate: true })
</script>

<template>
  <div class="context-mask" @click.self="emit('close')" @keydown.esc="emit('close')">
    <section class="context-dialog" role="dialog" aria-modal="true" aria-label="完整上下文">
      <header>
        <div><strong>完整上下文</strong><div class="context-note">{{ turnId }} · Precision 最终输出</div></div>
        <button type="button" class="btn btn-ghost" aria-label="关闭完整上下文" @click="emit('close')">关闭</button>
      </header>
      <div class="context-toolbar">
        <label v-if="detail?.iterations.length">迭代
          <select v-model="selected" :disabled="loading" @change="load(selected)">
            <option v-for="item in detail.iterations" :key="item.iteration" :value="item.iteration">
              {{ item.iteration }}{{ item.available ? '' : '（构建失败）' }}
            </option>
          </select>
        </label>
        <button class="btn btn-ghost" :class="{ active: view === 'text' }" @click="view = 'text'">文本</button>
        <button class="btn btn-ghost" :class="{ active: view === 'json' }" @click="view = 'json'">原始 JSON</button>
        <button class="btn btn-primary" :disabled="!build?.messages || loading" @click="copyFullText">{{ copied ? '已复制' : '复制全文' }}</button>
        <span v-if="build?.messages" class="context-note">{{ build.messages.length }} 条消息 · {{ build.tools.length }} 个工具</span>
      </div>
      <p v-if="loading" class="context-note">正在读取完整快照…</p>
      <p v-else-if="unavailable" class="context-note">{{ unavailable }}</p>
      <p v-if="error" class="msg msg-error">{{ error }}</p>
      <pre v-if="!loading && !unavailable && build?.messages" class="context-content">{{ fullText }}</pre>
      <details v-if="!loading && build?.records.length" class="context-decisions">
        <summary>条目定案与处理原因</summary><pre>{{ JSON.stringify(build.records, null, 2) }}</pre>
      </details>
    </section>
  </div>
</template>

<style scoped>
.context-mask { position: fixed; inset: 0; z-index: 40; background: #0008; display: flex; align-items: center; justify-content: center; padding: 20px; }
.context-dialog { width: min(1100px, 100%); max-height: 90vh; display: flex; flex-direction: column; gap: 12px; background: var(--panel); color: var(--text); border: 1px solid var(--border); border-radius: var(--radius); padding: 18px; overflow: auto; }
header, .context-toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
header { justify-content: space-between; }
.context-note { color: var(--text-dim); font-size: 12px; line-height: 1.8; }
.context-toolbar label { display: flex; align-items: center; gap: 8px; }
select { background: var(--bg); color: var(--text); border: 1px solid var(--border); padding: 5px; border-radius: 4px; }
.active { color: var(--primary); background: var(--primary-soft); }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font-family: var(--mono); font-size: 12px; line-height: 1.7; margin: 0; }
.context-content { flex: 1; min-height: 120px; overflow: auto; padding: 14px; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; }
.context-decisions summary { cursor: pointer; margin-bottom: 8px; }
@media (max-width: 640px) { .context-mask { padding: 8px; } .context-dialog { padding: 12px; max-height: 96vh; } }
</style>

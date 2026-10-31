<script setup>
import { onMounted, ref } from 'vue'

import { checkCorrectionEngine, checkHealth } from '../api'

const state = ref('loading') // loading | ok | degraded | error
const detail = ref('')

// 打开页面只做轻量检查：后端纯本地判断，不连模型、不花 token
async function refresh() {
  state.value = 'loading'
  detail.value = ''
  try {
    const data = await checkHealth()
    state.value = data?.status === 'ok' ? 'ok' : 'degraded'
    detail.value = data?.model
      ? `${data.model} · 已运行 ${data.uptime_seconds ?? '-'} 秒`
      : ''
  } catch (err) {
    state.value = 'error'
    detail.value = err?.message || '无法连接纠错服务'
  }
}

// 点一下 = 主动做一次深度自检：真跑一次纠错，会消耗 token
async function deepCheck() {
  state.value = 'loading'
  detail.value = '深度检测中…'
  try {
    const data = await checkCorrectionEngine()
    if (data?.status === 'ok') {
      state.value = 'ok'
      detail.value = `工具调用 ${data.tool_calls ?? 0} 次 · ${data.elapsed_ms ?? '-'} ms`
    } else {
      state.value = 'degraded'
      detail.value = data?.reason || '纠错链路未就绪'
    }
  } catch (err) {
    state.value = 'error'
    detail.value = err?.message || '无法连接纠错服务'
  }
}

const LABEL = {
  loading: '检测中…',
  ok: '服务正常',
  degraded: '服务降级',
  error: '连接失败',
}

onMounted(refresh)
defineExpose({ refresh })
</script>

<template>
  <div
    :class="['health', `health--${state}`]"
    :title="detail || '点击做一次深度自检（会消耗 token）'"
    @click="deepCheck"
  >
    <span class="health__dot"></span>
    <span class="health__text">{{ LABEL[state] }}</span>
    <span v-if="detail && state !== 'ok'" class="health__detail">{{ detail }}</span>
  </div>
</template>

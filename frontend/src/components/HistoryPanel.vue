<script setup>
import { ref } from 'vue'

import { records, removeResult, resultUrl } from '../results'

const error = ref('')

// 删记录是服务端的动作（它连着原文一起清）：删不掉就说一声，别让人以为删了
async function remove(id) {
  error.value = ''
  try {
    await removeResult(id)
  } catch (err) {
    error.value = err?.message || '删除失败，请稍后再试'
  }
}

// 耗时按毫秒存，挑一个顺眼的单位显示；老记录没存这两段就整段不显示。
function fmt(ms) {
  if (!Number.isFinite(ms) || ms < 0) return ''
  if (ms < 1000) return `${Math.round(ms)} 毫秒`
  const seconds = ms / 1000
  if (seconds < 60) return `${seconds.toFixed(1)} 秒`
  const minutes = Math.floor(seconds / 60)
  return `${minutes} 分 ${String(Math.round(seconds - minutes * 60)).padStart(2, '0')} 秒`
}

// 左边那块的角标：文档看后缀（PDF / DOCX），文本纠错没有后缀，统一叫 TXT。
function badge(row) {
  if (row.kind === 'text') return 'TXT'
  const matched = /\.([A-Za-z0-9]{1,4})$/.exec(row.filename || '')
  return matched ? matched[1].toUpperCase() : 'DOC'
}

// 解析 / 纠错各花了多久。只有一段时（文本纠错没有解析）就只显示那一段。
function durations(row) {
  const timings = row.timings || {}
  const out = []
  if (Number.isFinite(timings.parseMs)) out.push({ label: '解析', value: fmt(timings.parseMs) })
  if (Number.isFinite(timings.correctMs)) out.push({ label: '纠错', value: fmt(timings.correctMs) })
  if (!out.length && Number.isFinite(timings.totalMs)) out.push({ label: '合计', value: fmt(timings.totalMs) })
  return out
}
</script>

<template>
  <section class="panel">
    <div class="hist__head">
      <div>
        <h2 class="hist__title">纠错记录</h2>
      </div>
      <span v-if="records.length" class="hist__count">共 {{ records.length }} 条</span>
    </div>

    <div v-if="error" class="banner banner--warn">{{ error }}</div>

    <div v-if="!records.length" class="empty">
      <div class="empty__icon">🗂</div>
      <div>还没有纠错记录</div>
      <p class="muted">去「文档纠错」上传一份 PDF 或 Word 试试。</p>
    </div>

    <ul v-else class="hist-list">
      <li v-for="row in records" :key="row.id" class="hist-card">
        <span :class="['hist-card__badge', row.kind === 'text' ? 'hist-card__badge--text' : 'hist-card__badge--doc']">
          {{ badge(row) }}
        </span>

        <div class="hist-card__body">
          <div class="hist-card__top">
            <a class="hist-card__name" :href="resultUrl(row.id)" :title="row.filename">{{ row.filename }}</a>
            <span class="hist-card__time">{{ row.createdAt }}</span>
          </div>
          <div class="hist-card__meta">
            <span :class="['chip', row.kind === 'text' ? 'chip--text' : 'chip--doc']">
              {{ row.kind === 'text' ? '文本' : '文档' }}
            </span>
            <span class="stat"><b>{{ row.correctionCount }}</b> 处</span>
            <span class="sep">/</span>
            <span class="stat">涉及 <b>{{ row.unitsWithCorrection }}</b> 段</span>
            <template v-for="item in durations(row)" :key="item.label">
              <span class="sep">/</span>
              <span class="stat">{{ item.label }} <b>{{ item.value }}</b></span>
            </template>
            <span v-if="row.engineStatus === 'degraded'" class="chip chip--warn">引擎降级</span>
          </div>
        </div>

        <div class="hist-card__act">
          <a class="btn btn--primary btn--sm" :href="resultUrl(row.id)">打开</a>
          <button class="hist-card__del" title="删除这条记录" @click="remove(row.id)">删除</button>
        </div>
      </li>
    </ul>
  </section>
</template>

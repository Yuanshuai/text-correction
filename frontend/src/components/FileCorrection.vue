<script setup>
import { computed, onBeforeUnmount, ref } from 'vue'

import {
  correctFile, engineStatus, extractBlocks, extractDocument, extractHighlights,
  extractTimings, extractUnits,
} from '../api'
import { buildUnitMarks } from '../diff'
import { saveResult } from '../results'

// 能过转换服务的办公格式都收：后端先转成 PDF，再跟 PDF 走同一条解析/纠错/展示链路
const ACCEPT = [
  '.pdf', '.doc', '.docx', '.docm', '.rtf', '.odt', '.wps',
  '.xls', '.xlsx', '.xlsm', '.ods', '.et',
  '.ppt', '.pptx', '.odp', '.dps',
  '.txt', '.csv',
].join(',')

const file = ref(null)
const limit = ref(0)
const autoJump = ref(true)
const loading = ref(false)
const error = ref('')
const dragging = ref(false)
const inputRef = ref(null)
const done = ref(null)
const elapsed = ref(0)

// 纠错要几十秒到几分钟，等待期间留在当前页看秒表，不开空白页干等。
let timer = null

function stopTimer() {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
}

function startTimer() {
  stopTimer()
  elapsed.value = 0
  timer = setInterval(() => {
    elapsed.value += 1
  }, 1000)
}

onBeforeUnmount(stopTimer)

const elapsedText = computed(() => {
  const total = elapsed.value
  if (total < 60) return `${total} 秒`
  return `${Math.floor(total / 60)} 分 ${String(total % 60).padStart(2, '0')} 秒`
})

const loadingText = computed(() => `正在逐段纠错，已用 ${elapsedText.value}（大文件需要几分钟）`)

const fileMeta = computed(() => {
  if (!file.value) return ''
  const kb = file.value.size / 1024
  const size = kb > 1024 ? `${(kb / 1024).toFixed(1)} MB` : `${kb.toFixed(1)} KB`
  return `${file.value.name} · ${size}`
})

function pick() {
  inputRef.value?.click()
}

function onChange(event) {
  const chosen = event.target.files?.[0]
  if (chosen) setFile(chosen)
}

function onDrop(event) {
  dragging.value = false
  const chosen = event.dataTransfer?.files?.[0]
  if (chosen) setFile(chosen)
}

function setFile(chosen) {
  file.value = chosen
  error.value = ''
  done.value = null
}

async function submit() {
  if (!file.value) {
    error.value = '请先选择 PDF 或 Word 文档'
    return
  }
  loading.value = true
  error.value = ''
  done.value = null
  startTimer()
  const startedAt = performance.now()
  try {
    const payload = await correctFile(file.value, { limit: Number(limit.value) || 0 })
    // 处数按结果页同一套算法数（含「对不上原文就不标」的校验），
    // 这样记录列表里的处数和结果页页头的处数必然一致。
    const units = []
    let marksCount = 0
    for (const unit of extractUnits(payload)) {
      const { marks } = buildUnitMarks(unit.original_text, unit.errors)
      if (!marks.length) continue
      marksCount += marks.length
      units.push(unit)
    }
    // 后端的「原文件 + 改动位置」：有它结果页就铺 PDF、把改动框在原字上
    const doc = extractDocument(payload)
    const highlights = extractHighlights(payload)
    // 处数跟结果页清单同一口径：PDF 那套用的是后端给的 highlights，文字重排那套用现算的标记
    const correctionCount = highlights ? highlights.length : marksCount
    // 记录进后端的库（存不上就退回本机浏览器，这时链接换台机器打不开）
    const { id, stored } = await saveResult({
      filename: file.value.name,
      engineStatus: engineStatus(payload),
      units,
      // 全文分段：后端按正文顺序一起回（?full=true），结果页直接照着重放
      blocks: extractBlocks(payload),
      doc,
      highlights,
      // 解析、纠错各花了多久由后端分开计时回给我们；万一没回（比如后端没开 full），
      // 退而记一次客户端量到的总耗时，免得记录里那一行空着
      timings: extractTimings(payload) || { correctMs: Math.round(performance.now() - startedAt) },
      correctionCount,
      unitsWithCorrection: units.length,
    })
    done.value = { id, correctionCount, units: units.length, stored, pdf: Boolean(doc) }
    if (autoJump.value) {
      window.location.hash = `#/result/${id}`
    }
  } catch (err) {
    error.value = err?.message || '纠错失败'
  } finally {
    stopTimer()
    loading.value = false
  }
}
</script>

<template>
  <div class="panel">
    <div
      :class="['dropzone', { 'dropzone--active': dragging, 'dropzone--filled': file }]"
      @click="pick"
      @dragover.prevent="dragging = true"
      @dragleave.prevent="dragging = false"
      @drop.prevent="onDrop"
    >
      <input ref="inputRef" class="hidden-input" type="file" :accept="ACCEPT" @change="onChange" />
      <template v-if="file">
        <div class="dropzone__icon">📄</div>
        <div class="dropzone__name">{{ fileMeta }}</div>
        <div class="muted">点击可重新选择</div>
      </template>
      <template v-else>
        <div class="dropzone__icon">⬆️</div>
        <div class="dropzone__hint">点击选择或拖拽文件到此处</div>
      </template>
    </div>

    <div class="options">
      <label class="option">
        处理段落上限
        <input v-model.number="limit" class="option__input" type="number" min="0" step="1" />
        <span class="muted">0 表示不限制</span>
      </label>
      <label class="option option--check">
        <input v-model="autoJump" type="checkbox" />
        完成后打开结果页
      </label>
      <button class="btn btn--primary" :disabled="loading || !file" @click="submit">
        {{ loading ? `纠错中… ${elapsedText}` : '上传并纠错' }}
      </button>
    </div>

    <div v-if="loading" class="result__state">
      <span class="spinner"></span>
      {{ loadingText }}
    </div>
    <div v-else-if="error" class="banner banner--warn">{{ error }}</div>

    <div v-else-if="done" class="page-link">
      <span class="page-link__label">
        纠错完成：共 {{ done.correctionCount }} 处{{ done.pdf ? '' : ` · 涉及 ${done.units} 段` }}
      </span>
      <a class="link" :href="`#/result/${done.id}`">打开结果页</a>
        <span v-if="done.stored === 'local'" class="muted">（后端记录服务没开，这份结果只存在本机浏览器）</span>
    </div>
  </div>
</template>

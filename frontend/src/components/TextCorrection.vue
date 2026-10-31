<script setup>
import { computed, ref } from 'vue'

import { correctText, engineStatus, extractUnits } from '../api'
import { buildUnitMarks } from '../diff'
import { saveResult } from '../results'

const content = ref('')
const loading = ref(false)
const error = ref('')
const done = ref(null)

const charCount = computed(() => content.value.replace(/\s/g, '').length)

function snippet(text) {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > 24 ? `${flat.slice(0, 24)}…` : flat
}

async function submit() {
  const text = content.value.trim()
  if (!text) {
    error.value = '请先输入需要纠错的文本'
    return
  }
  loading.value = true
  error.value = ''
  done.value = null
  const startedAt = performance.now()
  try {
    const payload = await correctText(content.value)
    // 和文档纠错一样：结果先存本地，再跳同一个结果页（#/result/<id>）。
    // 处数按结果页同一套算法数（对不上原文的改动一律不标），
    // 这样「纠错记录」里的处数和结果页页头的处数必然一致。
    const units = []
    let correctionCount = 0
    for (const unit of extractUnits(payload)) {
      const { marks } = buildUnitMarks(unit.original_text, unit.errors)
      if (!marks.length) continue
      correctionCount += marks.length
      units.push(unit)
    }
    // 文本纠错的「全文」就是这段文本本身，后端原样回给我们，直接拿来当全文分段
    const blocks = units.map((unit) => ({ type: 'p', text: unit.original_text, errors: unit.errors }))
    // 记录进后端的库（存不上就退回本机浏览器，这时链接换台机器打不开）
    const { id, stored } = await saveResult({
      filename: `[文本] ${snippet(text)}`,
      kind: 'text',
      engineStatus: engineStatus(payload),
      units,
      blocks,
      // 文本纠错没有解析这一步，只有纠错：记这次的往返耗时（记录列表里的「纠错」）
      timings: { correctMs: Math.round(performance.now() - startedAt) },
      correctionCount,
      unitsWithCorrection: units.length,
    })
    done.value = { id, correctionCount, stored }
    window.location.hash = `#/result/${id}`
  } catch (err) {
    error.value = err?.message || '纠错失败'
  } finally {
    loading.value = false
  }
}

function clear() {
  content.value = ''
  error.value = ''
  done.value = null
}
</script>

<template>
  <div class="panel">
    <div class="editor">
      <textarea
        v-model="content"
        class="editor__area"
        placeholder="粘贴或输入需要纠错的文本（中文错别字、同音字、近音字……）"
        spellcheck="false"
      ></textarea>
      <div class="editor__foot">
        <span class="muted">共 {{ charCount }} 字</span>
        <div class="editor__actions">
          <button class="btn btn--ghost" @click="clear">清空</button>
          <button class="btn btn--primary" :disabled="loading || !content.trim()" @click="submit">
            {{ loading ? '纠错中…' : '开始纠错' }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="loading" class="result__state">
      <span class="spinner"></span>
      正在纠错，请稍候…
    </div>
    <div v-else-if="error" class="banner banner--warn">{{ error }}</div>

    <div v-else-if="done" class="page-link">
      <span class="page-link__label">
        纠错完成：共 {{ done.correctionCount }} 处
      </span>
      <a class="link" :href="`#/result/${done.id}`">打开结果页</a>
      <span v-if="done.stored === 'local'" class="muted">（后端记录服务没开，这份结果只存在本机浏览器）</span>
    </div>
  </div>
</template>

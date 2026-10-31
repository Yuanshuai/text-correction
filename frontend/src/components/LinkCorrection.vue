<script setup>
import { computed, onBeforeUnmount, ref } from 'vue'

import { correctByUrl } from '../api'
import { loadRecords } from '../results'

const url = ref('')
const autoJump = ref(true)
const loading = ref(false)
const error = ref('')
const done = ref(null)
const elapsed = ref(0)

// 大文件要按分钟等，等待期间留在当前页看秒表，不开空白页干等
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

const loadingText = computed(
  () => `正在取回文件并逐段纠错，已用 ${elapsedText.value}（大文件需要几分钟）`,
)

async function submit() {
  const value = url.value.trim()
  if (!value) {
    error.value = '请先粘贴文件链接'
    return
  }
  if (!/^https?:\/\//i.test(value)) {
    error.value = '链接要以 http:// 或 https:// 开头'
    return
  }
  loading.value = true
  error.value = ''
  done.value = null
  startTimer()
  try {
    const payload = await correctByUrl(value)
    // 记录是后端刚落的库：拿 record_id 直接跳结果页，不用自己再存一遍
    const id = payload?.record_id
    if (!id) {
      error.value = '纠错跑完了，但这次没能存下纠错记录（记录库不可用），拿不到结果页链接'
      return
    }
    done.value = { id, correctionCount: payload?.correction_count ?? 0 }
    // 顺手把「纠错记录」的列表刷新一下；失败不影响跳结果页
    loadRecords()
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
    <div class="linkbox">
      <label class="linkbox__label" for="link-url">文件链接</label>
      <input
        id="link-url"
        v-model="url"
        class="linkbox__input"
        type="url"
        placeholder="粘贴 PDF / Word 文件的链接（http:// 或 https://）"
        spellcheck="false"
        @keyup.enter="submit"
      />
      <div class="linkbox__foot">
        <span class="muted">
          PDF 直接解析；Word 等先转成 PDF 再走同一条链路。链接要能被本服务取到。
        </span>
        <div class="linkbox__actions">
          <label class="option option--check">
            <input v-model="autoJump" type="checkbox" />
            完成后打开结果页
          </label>
          <button class="btn btn--primary" :disabled="loading || !url.trim()" @click="submit">
            {{ loading ? `纠错中… ${elapsedText}` : '开始纠错' }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="loading" class="result__state">
      <span class="spinner"></span>
      {{ loadingText }}
    </div>
    <div v-else-if="error" class="banner banner--warn">{{ error }}</div>

    <div v-else-if="done" class="page-link">
      <span class="page-link__label">纠错完成：共 {{ done.correctionCount }} 处</span>
      <a class="link" :href="`#/result/${done.id}`">打开结果页</a>
    </div>
  </div>
</template>
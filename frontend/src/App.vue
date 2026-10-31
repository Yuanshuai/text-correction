<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'

import HealthBadge from './components/HealthBadge.vue'
import TextCorrection from './components/TextCorrection.vue'
import FileCorrection from './components/FileCorrection.vue'
import HistoryPanel from './components/HistoryPanel.vue'
import LinkCorrection from './components/LinkCorrection.vue'
import ResultPage from './components/ResultPage.vue'
import { fetchConfig } from './api'
import { initRecords } from './results'

// 配了外部解析服务时，纠错主要是外部系统走 API 调进来的：网页收起「文本纠错」「文档纠错」
// 这两条手工入口，只留「纠错记录」和「链接纠错」。开关由后端 /config 告诉前端；
// 后端没这个接口（老版本）或者拿不到，就按没配处理，三个页签照旧。
const parseService = ref(false)

const tab = ref('text')
const apiBase = import.meta.env.VITE_API_BASE || '（同源）'

// 极简 hash 路由：#/result/<id> 就是「结果页」，其余都留在标签页界面里。
const hash = ref(window.location.hash)
function onHashChange() {
  hash.value = window.location.hash
}
onMounted(async () => {
  window.addEventListener('hashchange', onHashChange)
  // 纠错记录都在后端库里：进页面先把列表拉回来（顺手把浏览器里攒的旧记录导进去）
  initRecords()
  try {
    const data = await fetchConfig()
    parseService.value = Boolean(data?.parse2json)
  } catch {
    // 后端老版本没有这个接口，或者服务不通：按没配处理
  }
  // 手工入口收起来之后，落地页就是纠错记录——那才是这个页面剩下的主要活儿
  if (parseService.value) tab.value = 'history'
})
onUnmounted(() => window.removeEventListener('hashchange', onHashChange))

const resultId = computed(() => {
  const matched = /^#\/result\/(.+)$/.exec(hash.value)
  return matched ? matched[1] : null
})

function openTab(next) {
  tab.value = next
}
</script>

<template>
  <ResultPage v-if="resultId" :key="resultId" :id="resultId" />

  <div v-else class="page">
    <header class="topbar">
      <div class="brand">
        <div class="brand__logo">纠</div>
        <div>
          <h1 class="brand__title">文本纠错</h1>
          <p class="brand__sub">PDF / Word / 文本 · 错别字检测与纠正</p>
        </div>
      </div>
      <HealthBadge />
    </header>

    <nav class="tabs">
      <button
        :class="['tabs__btn', { 'tabs__btn--active': tab === 'history' }]"
        @click="openTab('history')"
      >
        纠错记录
      </button>
      <template v-if="parseService">
        <button :class="['tabs__btn', { 'tabs__btn--active': tab === 'link' }]" @click="openTab('link')">
          链接纠错
        </button>
      </template>
      <template v-else>
        <button :class="['tabs__btn', { 'tabs__btn--active': tab === 'text' }]" @click="openTab('text')">
          文本纠错
        </button>
        <button :class="['tabs__btn', { 'tabs__btn--active': tab === 'file' }]" @click="openTab('file')">
          文档纠错
        </button>
      </template>
    </nav>

    <main class="content">
      <template v-if="parseService">
        <LinkCorrection v-show="tab === 'link'" />
      </template>
      <template v-else>
        <TextCorrection v-show="tab === 'text'" />
        <FileCorrection v-show="tab === 'file'" />
      </template>
      <HistoryPanel v-show="tab === 'history'" />
    </main>

    <footer class="foot">接口地址：{{ apiBase }}</footer>
  </div>
</template>

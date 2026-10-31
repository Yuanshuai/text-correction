<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'

import { buildDocView, TAG_LABEL } from '../diff'
import MarkedText from './MarkedText.vue'
import { getResult, setIgnored } from '../results'

const props = defineProps({
  id: { type: String, required: true },
})

// 页面上的基准缩放：PDF 坐标 × 这个数 = CSS 像素。后端给的方框就是 PDF 自己的坐标系
// （单位磅、左上角原点），跟画布用同一个缩放，框才落在原字上。
const DEFAULT_SCALE = 1.5
// 页面按阅读区宽度自适应：宽度吃满容器（两边不留灰边），极端窗口下夹在这个区间里
const MIN_SCALE = 0.4
const MAX_SCALE = 10
// 单个画布的像素数封顶：窗口越宽页面越大，采样率就按比例降下来，别把内存吃爆
const MAX_CANVAS_PIXELS = 12e6
const DPR = Math.min(window.devicePixelRatio || 1, 2)

// 点过 / 跳过去的那一条：这才是「我现在在看哪一处」。鼠标划到别的卡片上也不丢，
// 不然想点下一条的时候，刚看完的是哪条就找不着了。
const selected = ref(-1)
// 文档里要强调的那一处（就是那个蓝框）：只认选中，鼠标滑过不算数——框要一直停在点过的
// 那一处上，不然想看清刚才那一处，鼠标一挪就没了。
const active = computed(() => selected.value)

// 记录存在后端的库里：进页面按 id 取回来——所以这个链接换台机器、换个浏览器打开，
// 整份结果（原文 + 报告）都还在。
const record = ref(null)
const recordLoading = ref(true)
const recordError = ref('')
// 忽略状态没存上时的提示：别让界面显示的跟库里存的不一致
const ignoreError = ref('')

// 有后端托管的 PDF 就走「PDF 阅读器 + 高亮框」：跟直接打开原文件一个样，改动标在原位置。
// 没有（老记录、Word 没转成 PDF）就退回原来那套把文字重排出来的渲染。
const doc = computed(() => record.value?.doc || null)
const highlights = computed(() => record.value?.highlights || [])
const pdfMode = computed(() => Boolean(doc.value))

const view = computed(() => {
  const rec = record.value
  if (!rec) return { blocks: [], marks: [] }
  const blocks = rec.blocks?.length
    ? rec.blocks
    : rec.units.map((unit) => ({ type: 'p', text: unit.original_text, errors: unit.errors }))
  return buildDocView(blocks)
})

// 改动在全文里的位置（按 idx 索引）：报告条目靠它截出「原句」，读报告时不用来回跳。
const markByIndex = computed(() => new Map(view.value.marks.map((mark) => [mark.idx, mark])))

// 上下文截多长：改动前后各留这么多字，太长一行读不完，太短看不出是在说什么
const CTX_SPAN = 16
// 句读：碰到就停，不从句子中间截
const CTX_BREAK = new Set(['。', '！', '？', '；', '：', '\n', '\r'])

// 从改动所在的那一段原文里截一句上下文。位置跟后端贴框用的是同一套重放规则，所以
// 按 idx 能对上；对不上（老记录、后端丢过框）就不给上下文——宁可不给，也不能指错句子。
function contextOf(mark, expect) {
  if (!mark || typeof mark.text !== 'string' || !mark.text || !Number.isInteger(mark.at)) return null
  if (expect && (mark.tag !== expect.tag || mark.source !== (expect.before || '') || mark.target !== (expect.after || ''))) {
    return null
  }
  const text = mark.text
  const at = mark.at
  const len = Math.max(mark.len || 0, 0)
  const end0 = Math.min(text.length, at + len)
  let start = at
  let end = end0
  while (start > 0 && at - start < CTX_SPAN && !CTX_BREAK.has(text[start - 1])) start -= 1
  while (end < text.length && end - at < CTX_SPAN && !CTX_BREAK.has(text[end])) end += 1
  const cutHead = start > 0 && !CTX_BREAK.has(text[start - 1])
  const cutTail = end < text.length && !CTX_BREAK.has(text[end])
  return {
    pre: (cutHead ? '…' : '') + text.slice(start, at).replace(/^\s+/, ''),
    hit: text.slice(at, end0),
    post: text.slice(end0, end).replace(/\s+$/, '') + (cutTail ? '…' : ''),
  }
}

// 两种渲染共用一个清单口径，右侧报告和 ‹ › 跳转都只认它
const items = computed(() => {
  if (pdfMode.value) {
    return highlights.value.map((item) => {
      const entry = {
        key: item.index,
        tag: item.tag,
        before: item.original,
        after: item.corrected,
      }
      return { ...entry, context: contextOf(markByIndex.value.get(item.index), entry) }
    })
  }
  return view.value.marks.map((mark) => {
    const entry = {
      key: mark.idx,
      tag: mark.tag,
      before: mark.source,
      after: mark.target,
    }
    return { ...entry, context: contextOf(mark, null) }
  })
})

const total = computed(() => items.value.length)

// 手动忽略掉的那些（误报）：记在记录里，卡片上还能随时恢复
const ignoredKeys = computed(() => new Set(record.value?.ignored || []))
const ignoredCount = computed(() => items.value.filter((item) => ignoredKeys.value.has(item.key)).length)
// 待处理的处数（头上那颗徽章）：忽略掉的不算在里头
const activeCount = computed(() => total.value - ignoredCount.value)
// 只看已忽略的：误报点掉了，回头想翻出来恢复就切过来
const onlyIgnored = ref(false)

// 在这一档里把最后一条也恢复掉之后，「已忽略」就一条都不剩了——而那个切换按钮只在
// 「还有已忽略的」时候才画；不自己退回去就会停在一个空报告上、连返回的入口都不见了。
watch(ignoredCount, (count) => {
  if (!count) onlyIgnored.value = false
})

async function toggleIgnored(key) {
  const next = new Set(ignoredKeys.value)
  const wasIgnored = next.has(key)
  if (wasIgnored) next.delete(key)
  else next.add(key)
  ignoreError.value = ''
  try {
    const updated = await setIgnored(props.id, [...next])
    if (updated) record.value = updated
  } catch (err) {
    // 存不上就照旧显示：宁可这一下没生效，也不能显示出跟库里不一致的状态
    ignoreError.value = err?.message || '忽略状态没能存上，请稍后再试'
    return
  }
  // 把正在看的那一处忽略了就往后挪一处——不然选中态停在一条已经作废的改动上
  if (!wasIgnored && selected.value === key) {
    const order = ordered.value
    const at = order.findIndex((item) => item.key === key)
    const after = order.slice(at + 1)
      .concat(order.slice(0, at))
      .find((item) => !next.has(item.key))
    if (after) focus(after.key)
    else selected.value = -1
  }
}

// 报告有两种看法：按文档顺序（默认，从头改到尾）；按类型（同类的排在一起，
// 比如统一过一遍所有删字）。分组只是把同一个列表换个排法，条目本身一模一样。
const GROUP_ORDER = ['replace', 'delete', 'insert']
const groupMode = ref('doc')

// 列表里要显示的条目：判成误报忽略掉的那些，平时就不出现在报告里（当它不存在）；
// 要恢复就去「已忽略」那一档，切过去之后反过来只显示忽略掉的那些。
const shownItems = computed(() => (
  onlyIgnored.value
    ? items.value.filter((item) => ignoredKeys.value.has(item.key))
    : items.value.filter((item) => !ignoredKeys.value.has(item.key))
))

const groups = computed(() => {
  if (groupMode.value !== 'tag') return [{ key: 'doc', tag: '', items: shownItems.value }]
  return GROUP_ORDER
    .map((tag) => ({ key: tag, tag, items: shownItems.value.filter((item) => item.tag === tag) }))
    .filter((group) => group.items.length)
})

// ‹ › 和键盘走的是「眼前这个顺序」，不是固定按文档顺序——换了排法就得跟着换。
// 忽略掉的那些本来就不在这个顺序里（见 shownItems），所以翻下一处不会停在它上面。
const ordered = computed(() => groups.value.flatMap((group) => group.items))
const navTotal = computed(() => ordered.value.length)

function setGroupMode(mode) {
  if (groupMode.value === mode) return
  groupMode.value = mode
  // 换了排法，选中的那条位置变了，把它重新带回视野里
  nextTick(() => {
    if (selected.value >= 0) ensureRailVisible(selected.value)
  })
}

const current = computed(() => {
  const index = ordered.value.findIndex((item) => item.key === selected.value)
  return index < 0 ? 0 : index + 1
})

const hitsByPage = computed(() => {
  const map = new Map()
  for (const item of highlights.value) {
    const list = map.get(item.page)
    if (list) list.push(item)
    else map.set(item.page, [item])
  }
  return map
})

function hitsOf(index) {
  return hitsByPage.value.get(index) || []
}

// 每一页可以有自己的缩放（页面尺寸不一的文档），所以高亮框要按它所在那一页换算
function pageScaleOf(index) {
  return pages.value[index]?.scale || DEFAULT_SCALE
}

function rectStyle(rect, index) {
  const scale = pageScaleOf(index)
  return {
    left: `${rect[0] * scale}px`,
    top: `${rect[1] * scale}px`,
    width: `${Math.max((rect[2] - rect[0]) * scale, 2)}px`,
    height: `${(rect[3] - rect[1]) * scale}px`,
  }
}

// ── PDF 阅读器 ──

const pages = shallowRef([])
const pageNo = ref(1)
const loading = ref(false)
const docError = ref('')
const scroller = ref(null)
const canvases = new Map()
const rendered = new Set()
let pdfDoc = null
let pageObjects = []
// 每一页在滚动内容里的纵向位置（CSS 像素），由 DOM 实测量出来，焦点跳转靠它算目标位置
let pageTops = []
let scheduled = false

function setCanvas(index, el) {
  if (el) canvases.set(index, el)
  else canvases.delete(index)
}

async function renderPage(index) {
  if (!pdfDoc || rendered.has(index)) return
  const canvas = canvases.get(index)
  const size = pages.value[index]
  const page = pageObjects[index]
  if (!canvas || !size || !page) return
  rendered.add(index)
  try {
    const area = Math.max(size.width * size.height, 1)
    const renderDpr = Math.max(1, Math.min(DPR, Math.sqrt(MAX_CANVAS_PIXELS / area)))
    const viewport = page.getViewport({ scale: (size.scale || DEFAULT_SCALE) * renderDpr })
    canvas.width = Math.floor(viewport.width)
    canvas.height = Math.floor(viewport.height)
    canvas.style.width = `${size.width}px`
    canvas.style.height = `${size.height}px`
    await page.render({ canvasContext: canvas.getContext('2d'), viewport }).promise
  } catch (err) {
    // 这一页没画出来就放回队列，滚回来时再试
    rendered.delete(index)
  }
}

// 页面铺满阅读区：每一页按自己的原始宽度算缩放率——文档里尺寸不一样时（混了 A4 / A5、
// 横竖版），小页面不会被大页面连累出灰边，也是铺满容器。
function pageScale(page) {
  const root = scroller.value
  if (!root) return DEFAULT_SCALE
  const available = root.clientWidth
  const width = page.getViewport({ scale: 1 }).width
  if (!available || !width) return DEFAULT_SCALE
  return Math.min(Math.max(available / width, MIN_SCALE), MAX_SCALE)
}

// 每一页的 CSS 尺寸：宽度正好是容器宽度（不留缝），高度按同一个缩放换算
function applySizes() {
  const root = scroller.value
  const available = root ? root.clientWidth : 0
  pages.value = pageObjects.map((page) => {
    const scale = pageScale(page)
    const viewport = page.getViewport({ scale })
    return {
      width: available || viewport.width,
      height: viewport.height,
      scale,
    }
  })
}

// 只画视口附近的页：几十页的合同一次性全画完既占内存又卡；高亮框本身很轻，一次铺满不卡。
// 这里按滚动位置自己算，不用 IntersectionObserver——在没有合成器的窗口里它会把所有页都
// 判成「可见」，几十个画布一起铺出来能吃掉几百兆。
const PAD = 1200
const KEEP = 5

function measure() {
  const root = scroller.value
  if (!root) return
  const base = root.getBoundingClientRect().top - root.scrollTop
  pageTops = [...root.querySelectorAll('[data-page]')]
    .map((el) => el.getBoundingClientRect().top - base)
}

// 滚远的页把画布放掉（宽度高度清零就是真的释放了）：留着几十张画布会一直占着内存
function releasePage(index) {
  if (!rendered.has(index)) return
  const canvas = canvases.get(index)
  if (canvas) {
    canvas.width = 0
    canvas.height = 0
  }
  rendered.delete(index)
}

// 视口上方三分之一处落在哪一页，就算当前页——比「顶边对齐」更贴近眼睛的感觉
function updatePageNo() {
  const root = scroller.value
  if (!root || !pageTops.length) return
  const probe = root.scrollTop + root.clientHeight * 0.35
  let index = 0
  for (let i = 0; i < pageTops.length; i += 1) {
    if (pageTops[i] > probe) break
    index = i
  }
  pageNo.value = index + 1
}

function paint() {
  const root = scroller.value
  if (!root || !pageTops.length) return
  updatePageNo()
  const from = root.scrollTop - PAD
  const to = root.scrollTop + root.clientHeight + PAD
  const keep = root.clientHeight * KEEP
  for (let index = 0; index < pageTops.length; index += 1) {
    const top = pageTops[index]
    const bottom = top + (pages.value[index]?.height || 0)
    if (bottom >= from && top <= to) {
      renderPage(index)
      continue
    }
    if (bottom < root.scrollTop - keep || top > root.scrollTop + root.clientHeight + keep) {
      releasePage(index)
    }
  }
}

// 翻页：把目标页的顶边对到阅读区上方，看起来就是「换了一页」
function gotoPage(n) {
  const count = pages.value.length
  const root = scroller.value
  if (!count || !root || pageTops.length < count) return
  const index = Math.min(Math.max(n, 1), count) - 1
  root.scrollTop = Math.max(0, pageTops[index] - 8)
  paint()
}

function onScroll() {
  if (scheduled) return
  scheduled = true
  requestAnimationFrame(() => {
    scheduled = false
    paint()
  })
}

let resizing = false
let lastWidth = 0

function onResize() {
  if (resizing) return
  const root = scroller.value
  const width = root ? root.clientWidth : 0
  if (width === lastWidth) {
    // 宽度没变（比如只是窗口高度变了）：量一下位置接着画就行
    measure()
    paint()
    return
  }
  // 宽度变了：每一页按新宽度重排，画布全部作废，回到视口附近再按需画
  resizing = true
  lastWidth = width
  applySizes()
  rendered.clear()
  for (const canvas of canvases.values()) {
    canvas.width = 0
    canvas.height = 0
  }
  nextTick(() => {
    measure()
    paint()
    resizing = false
  })
}

function setupViewer() {
  const root = scroller.value
  if (!root) return
  root.addEventListener('scroll', onScroll, { passive: true })
  window.addEventListener('resize', onResize)
  measure()
  paint()
}

async function loadPdf() {
  pages.value = []
  pageObjects = []
  canvases.clear()
  rendered.clear()
  docError.value = ''
  if (!doc.value) return
  loading.value = true
  try {
    const { loadDocument } = await import('../pdfdoc')
    pdfDoc = await loadDocument(doc.value.pdfUrl)
    for (let number = 1; number <= pdfDoc.numPages; number += 1) {
      pageObjects.push(await pdfDoc.getPage(number))
    }
    loading.value = false
    // 阅读区这时候才排完版、量得到宽度：按它把每一页放大到铺满整个容器
    await nextTick()
    lastWidth = scroller.value ? scroller.value.clientWidth : 0
    applySizes()
    await nextTick()
    setupViewer()
  } catch (err) {
    loading.value = false
    docError.value = err?.message || '文档加载失败'
  }
}

// ── 定位 ──

// 报告可能很长，跳过去的那一条要是在视野外，得把它带进来，不然不知道跳到了哪一处
function ensureRailVisible(key) {
  const railEl = document.querySelector(`[data-rail="${key}"]`)
  if (railEl) railEl.scrollIntoView({ block: 'nearest', behavior: 'auto' })
}

function focus(key) {
  selected.value = key
  ensureRailVisible(key)
  if (pdfMode.value) {
    const item = highlights.value.find((hl) => hl.index === key)
    if (!item) return
    const root = scroller.value
    if (root && pageTops[item.page] !== undefined) {
      const rect = item.rects[0]
      const size = pages.value[item.page]
      const center = pageTops[item.page] + (rect
        ? ((rect[1] + rect[3]) / 2) * (size?.scale || DEFAULT_SCALE)
        : (size ? size.height / 2 : 0))
      root.scrollTop = Math.max(0, center - root.clientHeight / 2)
    }
    renderPage(item.page)
    paint()
    return
  }
  const el = document.querySelector(`[data-mark="${key}"]`)
  if (el) el.scrollIntoView({ block: 'center', behavior: 'auto' })
}

// 报告里点一下 = 选中这一处，框就停在它上面；再点一次同一张 = 取消选中，框跟着撤掉。
// 左右箭头/‹ › 不走这里：那是「换一处看」，本来就不会去取消选中。
function selectItem(key) {
  if (selected.value === key) {
    selected.value = -1
    return
  }
  focus(key)
}

// ← / ↑ 上一处，→ / ↓ 下一处；到头了就绕回另一头
function step(delta) {
  const list = ordered.value
  if (!list.length) return
  const index = list.findIndex((item) => item.key === selected.value)
  const next = index < 0
    ? (delta > 0 ? 0 : list.length - 1)
    : (index + delta + list.length) % list.length
  focus(list[next].key)
}

const KEYS = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }

function onKey(event) {
  if (event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return
  const target = event.target
  if (target?.tagName === 'INPUT' || target?.tagName === 'TEXTAREA' || target?.isContentEditable) return
  if (pdfMode.value && (event.key === 'PageDown' || event.key === 'PageUp')) {
    event.preventDefault()
    gotoPage(pageNo.value + (event.key === 'PageDown' ? 1 : -1))
    return
  }
  const delta = KEYS[event.key]
  if (!delta) return
  event.preventDefault()
  step(delta)
}

onMounted(async () => {
  window.addEventListener('keydown', onKey)
  try {
    record.value = await getResult(props.id)
  } catch (err) {
    recordError.value = err?.message || '这条纠错记录没能取回来'
  }
  recordLoading.value = false
  // 等记录渲染出来、阅读区排完版，再去开 PDF
  await nextTick()
  if (pdfMode.value) loadPdf()
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKey)
  window.removeEventListener('resize', onResize)
  scroller.value?.removeEventListener('scroll', onScroll)
  for (const page of pageObjects) {
    try {
      page.cleanup()
    } catch (err) {
      // 页面已经卸载过了，不用管
    }
  }
  pdfDoc?.destroy()
  pdfDoc = null
})

// 回退：优先用浏览器历史，直接开链接进来的就回首页
function back() {
  if (window.history.length > 1) window.history.back()
  else window.location.hash = ''
}

function display(value) {
  if (value === '' || value === null || value === undefined) return '∅'
  if (value === '\n') return '⏎'
  if (value === ' ') return '␣'
  return value
}
</script>

<template>
  <div v-if="recordLoading" class="rp__empty">
    <div class="empty__icon"><span class="spinner"></span></div>
    <div>正在打开纠错记录…</div>
  </div>

  <div v-else-if="!record" class="rp__empty">
    <div class="empty__icon">🗂</div>
    <div>这条纠错记录不存在</div>
    <p v-if="recordError" class="muted">{{ recordError }}</p>
    <p v-else class="muted">记录存在服务端的数据库里，可能是链接写错了，或者这条记录已经被删掉了。</p>
    <button class="btn btn--primary" @click="back">返回</button>
  </div>

  <div v-else :class="['rp', { 'rp--fit': pdfMode }]">
    <header class="rp__bar">
      <button class="btn btn--ghost" @click="back">← 返回</button>
      <div class="rp__id">
        <div class="rp__file">{{ record.filename }}</div>
      </div>

      <div v-if="total" class="rp__nav">
        <button class="rp__nav-btn" :title="'上一处（←／↑）'" @click="step(-1)">‹</button>
        <span class="rp__nav-pos">{{ current || '–' }} / {{ navTotal }} 处</span>
        <button class="rp__nav-btn" :title="'下一处（→／↓）'" @click="step(1)">›</button>
      </div>
    </header>

    <div v-if="ignoreError" class="banner banner--warn rp__notice">{{ ignoreError }}</div>

    <div v-if="!total" class="rp__none">
      <div class="empty__icon">✓</div>
      <div>未检出需要纠正的字</div>
    </div>

    <div v-else class="rp__body">
      <!-- 原文件什么样就什么样：PDF 一页页画出来，改动直接框在原字上 -->
      <div v-if="pdfMode" class="rpv">
        <div v-if="docError" class="rpv__tip">{{ docError }}</div>
        <div v-else-if="loading" class="rpv__tip"><span class="spinner"></span> 正在打开文档…</div>
        <div v-else ref="scroller" class="rpv__scroll">
          <div
            v-for="(page, index) in pages"
            :key="index"
            class="rpv__page"
            :data-page="index"
            :style="{ width: `${page.width}px`, height: `${page.height}px` }"
          >
            <canvas :ref="(el) => setCanvas(index, el)" class="rpv__canvas"></canvas>
            <div class="rpv__layer">
              <template v-for="item in hitsOf(index)" :key="item.index">
                <span
                  v-for="(rect, ri) in item.rects"
                  :key="`${item.index}-${ri}`"
                  :data-hl="ri === 0 ? item.index : null"
                  :class="['rpv__hl', `rpv__hl--${item.tag}`, { 'is-active': item.index === active, 'is-ignored': ignoredKeys.has(item.index) }]"
                  :style="rectStyle(rect, index)"
                ></span>
              </template>
            </div>
          </div>
        </div>

        <!-- 左下角：当前页 / 共几页，点箭头直接翻页 -->
        <div v-if="pages.length" class="rpv__foot">
          <span class="rpv__pg-no">第 {{ pageNo }} / {{ pages.length }} 页</span>
          <button class="rpv__pg" :disabled="pageNo <= 1" title="上一页（PageUp）" @click="gotoPage(pageNo - 1)">‹</button>
          <button class="rpv__pg" :disabled="pageNo >= pages.length" title="下一页（PageDown）" @click="gotoPage(pageNo + 1)">›</button>
        </div>
      </div>

      <!-- 兜底：没有托管 PDF 的记录（老记录、Word 没转成 PDF）还是把文字重排出来标色 -->
      <div v-else class="rp__main">
        <template v-for="block in view.blocks" :key="block.key">
          <article v-if="block.kind !== 'table'" :class="['rp__doc', { 'rp__doc--hit': block.count }]">
            <p class="rp__text">
              <MarkedText
                :ignored="ignoredKeys"
                :tokens="block.tokens"
                :text="block.text"
                :active="active"
                @pick="selectItem"
              />
            </p>
          </article>

          <!-- 表格：纠错时一格一个单元，展示时按 table / row / colspan 拼回一张表。
               列一多就压成一字一行了，所以窄表撑满、宽表给个最小列宽，横向滚动看。 -->
          <div v-else class="rp__tbl-wrap">
            <table class="rp__tbl" :style="block.cols > 6 ? { minWidth: block.cols * 88 + 'px' } : null">
              <tbody>
                <tr v-for="(row, ri) in block.rows" :key="ri">
                  <td
                    v-for="(cell, ci) in row.cells"
                    :key="ci"
                    :colspan="cell.colspan"
                    :rowspan="cell.rowspan"
                    :class="['rp__cell', { 'rp__cell--hit': cell.count }]"
                  >
                    <MarkedText
                      :ignored="ignoredKeys"
                      :tokens="cell.tokens"
                      :text="cell.text"
                      :active="active"
                      @pick="selectItem"
                    />
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </div>

      <aside class="rp__rail">
        <div class="rp__rail-head">
          <span class="rp__rail-title">纠错报告</span>
          <div class="rp__rail-modes" role="group" aria-label="报告排序方式">
            <button
              :class="['rp__mode', { 'is-on': groupMode === 'doc' }]"
              title="按改动在文档里的先后顺序排"
              @click="setGroupMode('doc')"
            >按顺序</button>
            <button
              :class="['rp__mode', { 'is-on': groupMode === 'tag' }]"
              title="同类的排在一起：改字 / 删字 / 增字"
              @click="setGroupMode('tag')"
            >按类型</button>
          </div>
          <span class="rp__rail-count">{{ activeCount }} 处</span>
        </div>
        <!-- 忽略过误报才出现：点一下只看已忽略的那些，再点回来 -->
        <div v-if="ignoredCount" class="rp__rail-filter">
          <button
            :class="['rp__filter', { 'is-on': onlyIgnored }]"
            @click="onlyIgnored = !onlyIgnored"
          >{{ onlyIgnored ? '← 返回全部' : `已忽略 ${ignoredCount} 处` }}</button>
        </div>
        <div class="rp__rail-list">
          <template v-for="group in groups" :key="group.key">
            <!-- 按类型看的时候，每一类上面挂一行小结；按顺序看时没有这一行 -->
            <div v-if="groupMode === 'tag'" class="rp__group">
              <i :class="['rp__dot', `rp__dot--${group.tag}`]"></i>
              <span class="rp__group-name">{{ TAG_LABEL[group.tag] }}</span>
              <span class="rp__group-note">{{ group.items.length }} 处</span>
            </div>
            <button
              v-for="item in group.items"
              :key="item.key"
              :class="['rp__item', `rp__item--${item.tag}`, { 'is-selected': item.key === selected, 'is-ignored': ignoredKeys.has(item.key) }]"
              :data-rail="item.key"
              @click="selectItem(item.key)"
            >
              <span class="rp__row">
                <span class="rp__pair">
                  <span class="rp__val">{{ display(item.before) }}</span>
                  <span class="rp__arrow">→</span>
                  <span class="rp__val rp__val--after">{{ display(item.after) }}</span>
                </span>
                <!-- 误报就点掉：卡片自己留着（灰着），随时能恢复 -->
                <button
                  :class="['rp__act', { 'is-restore': ignoredKeys.has(item.key) }]"
                  :title="ignoredKeys.has(item.key) ? '把这一处恢复成要改的' : '这一处是误报，不用改'"
                  @click.stop="toggleIgnored(item.key)"
                >{{ ignoredKeys.has(item.key) ? '恢复' : '忽略' }}</button>
              </span>
              <!-- 原句：报告里直接读出改动在什么话里，省得靠记忆来回跳 -->
              <span v-if="item.context" class="rp__ctx">
                <span>{{ item.context.pre }}</span>
                <i v-if="item.context.hit" :class="['rp__ctx-hit', `rp__ctx-hit--${item.tag}`]">{{ item.context.hit }}</i>
                <i v-else class="rp__ctx-caret"></i>
                <span>{{ item.context.post }}</span>
              </span>
            </button>
          </template>
        </div>
      </aside>
    </div>
  </div>
</template>

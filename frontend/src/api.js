// 纠错服务 HTTP 封装：
//   GET  /health              轻量存活检查（不花 token，容器健康检查用这个）
//   GET  /health/correction   纠错链路深度自检（真跑一次纠错，花 token）
//   GET  /config             后端能力开关（有没有配外部解析服务）
//   POST /correction_text     {"content": "..."}     单段文本纠错
//   POST /correction          multipart file=...     PDF / Word 文档纠错
//   POST /openapi/contract-audit/web-correct-DocParser  multipart fileUrl=...  按链接纠错（对外的 DocParser）
//   GET  /document/<id>                              结果页要展示的那份 PDF
//   GET  /records            纠错记录列表（存后端的 MySQL）
//   POST /records            存一条纠错记录
//   GET  /records/<id>       取一条记录的整份内容（结果页用）
//   PUT  /records/<id>/ignored   标记哪些是误报
//   DELETE /records/<id>     删一条记录
//
// /correction 的 results 只回**检出错字的段落**（老调用方按这个用）；
// 前端要展示整篇文档，所以额外带 ?full=true：后端会多回一份按正文顺序的 blocks、
// 一份「改动标在页面哪儿」的 highlights，以及托管起来的 PDF 地址（Word 也会先转成 PDF，
// 这样结果页跟直接打开原文件一模一样）。

const API_BASE = (import.meta.env.VITE_API_BASE || '').replace(/\/+$/, '')

function endpoint(path) {
  return `${API_BASE}${path}`
}

async function readBody(res) {
  const text = await res.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return { detail: text }
  }
}

function errorMessage(data, res) {
  if (data && typeof data === 'object') {
    if (typeof data.detail === 'string') return data.detail
    if (typeof data.message === 'string') return data.message
  }
  return `请求失败（HTTP ${res.status}）`
}

async function parse(res) {
  const data = await readBody(res)
  if (!res.ok) {
    const error = new Error(errorMessage(data, res))
    // 调用方要按状态码分情况处理（比如记录存不上时是退回本地还是直接报错）
    error.status = res.status
    throw error
  }
  return data
}

// 轻量存活检查：后端不连模型、不花 token。页面打开和容器健康检查都走这条。
export async function checkHealth() {
  const res = await fetch(endpoint('/health'))
  return parse(res)
}

// 深度自检：真跑一次纠错，看模型会不会调工具、内置错别字改没改对。
// 会消耗 token，只在用户主动点服务徽标时调。
export async function checkCorrectionEngine() {
  const res = await fetch(endpoint('/health/correction'))
  return parse(res)
}

// 后端能力开关。配了外部解析服务（DocParser）时，网页只留「链接纠错」和「纠错记录」：
// 那种部署下纠错主要是外部系统走 API 调进来的，网页再摆手工上传入口只会让人误用。
// 拿不到（后端老版本、网络不通）就当没配，两个手工入口照旧摆出来。
export async function fetchConfig() {
  const res = await fetch(endpoint('/config'))
  return parse(res)
}

// 按链接调用对外接口 DocParser：配了解析服务时链接原样转交给它，没配则由后端下载后走同一条链路，
// 并把这次结果存成一条纠错记录。记录由后端落库，所以这里不用再存一遍，拿 record_id 跳结果页就行。
export async function correctByUrl(fileUrl) {
  const form = new FormData()
  form.append('fileUrl', fileUrl)
  const res = await fetch(endpoint('/openapi/contract-audit/web-correct-DocParser?full=true'), {
    method: 'POST',
    body: form,
  })
  return parse(res)
}

export async function correctText(content) {
  const res = await fetch(endpoint('/correction_text'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  })
  return parse(res)
}

// 文件纠错：PDF / Word -> 逐段纠错结果（full=true 时还带一份全文分段）
export async function correctFile(file, { limit = 0, debug = false, full = true } = {}) {
  const form = new FormData()
  form.append('file', file)

  const query = new URLSearchParams()
  if (limit > 0) query.set('limit', String(limit))
  if (debug) query.set('debug', 'true')
  query.set('full', full ? 'true' : 'false')
  const suffix = `?${query.toString()}`

  const res = await fetch(endpoint(`/correction${suffix}`), {
    method: 'POST',
    body: form,
  })
  return parse(res)
}

export function extractUnits(payload) {
  const raw = payload?.results ?? payload?.data?.results
  if (!raw) return []
  const list = Array.isArray(raw) ? raw : [raw]
  return list.map((unit) => ({
    original_text: unit?.original_text ?? '',
    correct_text: unit?.correct_text ?? '',
    wrong_ids: unit?.wrong_ids ?? [],
    errors: unit?.errors ?? [],
  }))
}

// 后端给的全文分段，按正文顺序。段落是 { type: 'p', text, errors }；表格单元格是
// { type: 'row', text, errors, table, row, col, colspan, rowspan }——纠错一个单元格一个
// 单元，展示时靠后几个字段把格子拼回一张表，所以这里要原样带过去。
// 老记录/后端没开 full 时没有这一段，结果页退回「只列检出错字的段落」。
export function extractBlocks(payload) {
  const raw = payload?.blocks
  if (!Array.isArray(raw)) return null
  return raw.map((block) => {
    const out = {
      type: block?.type === 'row' ? 'row' : 'p',
      text: block?.text ?? '',
      errors: block?.errors ?? [],
    }
    if (Number.isInteger(block?.table)) {
      out.table = block.table
      out.row = Number(block?.row) || 0
      out.col = Number(block?.col) || 0
      out.colspan = Number(block?.colspan) || 1
      out.rowspan = Number(block?.rowspan) || 1
    }
    return out
  })
}

// 解析 / 纠错各花了多久（毫秒）。跟 blocks 一样只在 ?full=true 时回；老记录、或者后端
// 没开 full 时没有这一段，记录列表就不显示耗时。
export function extractTimings(payload) {
  const raw = payload?.timings
  if (!raw || typeof raw !== 'object') return null
  const out = {}
  for (const [key, field] of [
    ['parseMs', 'parse_ms'],
    ['correctMs', 'correct_ms'],
    ['totalMs', 'total_ms'],
  ]) {
    const value = Number(raw[field])
    if (Number.isFinite(value)) out[key] = value
  }
  return Object.keys(out).length ? out : null
}

// 结果页要展示的原文件：上传时后端把 PDF 落盘（原始 PDF，或 Office 转出来的那份），
// 这里换成前端能直接取的口径。老记录、或后端没转成 PDF 时是 null，
// 结果页就退回「把文字重排出来」那套渲染。
export function extractDocument(payload) {
  const docId = payload?.doc_id
  if (typeof docId !== 'string' || !docId) return null
  const raw = payload?.pdf_url
  const path = typeof raw === 'string' && raw ? raw : `/document/${docId}`
  return {
    docId,
    pdfUrl: /^https?:/i.test(path) ? path : endpoint(path.startsWith('/') ? path : `/${path}`),
    sourceKind: payload?.source_kind || 'pdf',
  }
}

// 页面高亮框：坐标就是 PDF 自己的坐标系（左上角原点、单位磅），跟画布用同一个缩放，
// 所以框一定贴在原字上。一处改动可能占好几个框（跨行、跨词），一个框一个 div 铺上去。
export function extractHighlights(payload) {
  const raw = payload?.highlights
  if (!Array.isArray(raw)) return null
  const round = (value) => Math.round(Number(value) * 10) / 10
  return raw.map((item, index) => ({
    index: Number.isInteger(item?.index) ? item.index : index,
    page: Number(item?.page) || 0,
    tag: item?.tag === 'delete' || item?.tag === 'insert' ? item.tag : 'replace',
    rects: Array.isArray(item?.rects)
      ? item.rects
          .filter((rect) => Array.isArray(rect) && rect.length >= 4)
          .map((rect) => rect.slice(0, 4).map(round))
      : [],
    original: item?.original ?? '',
    corrected: item?.corrected ?? '',
  }))
}

export function engineStatus(payload) {
  return payload?.engine_status === 'degraded' ? 'degraded' : 'ok'
}

// ── 纠错记录（存后端的 MySQL）──
//
// 后端没配库（MYSQL_DSN）时这几个接口回 503，调用方（results.js）据此退回浏览器本地存储，
// 页面照样能用，只是链接换台机器打不开。

export async function listRecords({ limit = 50, offset = 0 } = {}) {
  const res = await fetch(endpoint(`/records?limit=${limit}&offset=${offset}`))
  return parse(res)
}

export async function createRecord(record) {
  const res = await fetch(endpoint('/records'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(record),
  })
  return parse(res)
}

export async function fetchRecord(id) {
  const res = await fetch(endpoint(`/records/${encodeURIComponent(id)}`))
  return parse(res)
}

export async function putIgnored(id, keys) {
  const res = await fetch(endpoint(`/records/${encodeURIComponent(id)}/ignored`), {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ignored: keys }),
  })
  return parse(res)
}

export async function deleteRecord(id) {
  const res = await fetch(endpoint(`/records/${encodeURIComponent(id)}`), { method: 'DELETE' })
  return parse(res)
}

// 结果页要展示的原文：记录里存的是相对地址（/document/<id>），这里按当前服务地址补全，
// 所以同一个链接换台机器打开，取到的还是它自己那台后端上的原文。
export function documentUrl(path) {
  if (typeof path !== 'string' || !path) return ''
  return /^https?:/i.test(path) ? path : endpoint(path.startsWith('/') ? path : `/${path}`)
}

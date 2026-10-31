// 纠错结果与「纠错记录」。
// 记录存在后端的 MySQL 里（后端没配库时回 503，这里自动退回浏览器本地存储），
// 所以同一个结果链接换台机器、换个浏览器都打得开，也分享得出去。
import { computed, ref } from 'vue'

import {
  createRecord, deleteRecord, documentUrl, fetchRecord, listRecords, putIgnored,
} from './api'

const STORAGE_KEY = 'text-correction:results:v1'
// 浏览器本地最多留几条（离线兜底用的那份，跟服务端的量级无关）
const MAX_LOCAL = 50

// 记录存哪儿：'server' = 后端数据库，'local' = 退回了本机浏览器
export const storage = ref('server')
// 服务端那份列表（摘要，不带全文）
const remote = ref([])
// 本机浏览器里那份：没配库时的兜底，也是还没导进库的旧记录
const local = ref(readLocal())
// 当前打开的那份记录（结果页要用的整份内容），忽略状态改完直接换掉它
const cache = new Map()

// ── 本机浏览器那份 ──

function readLocal() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    const data = raw ? JSON.parse(raw) : []
    return Array.isArray(data) ? data : []
  } catch {
    return []
  }
}

function persistLocal() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(local.value))
    return true
  } catch {
    // 存不下（多半是配额满了）：丢掉最旧的再试，还不行就放弃写入
    while (local.value.length > 1) {
      local.value = local.value.slice(0, -1)
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(local.value))
        return true
      } catch {
        /* 继续丢 */
      }
    }
    return false
  }
}

function newLocalId() {
  const now = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`
  return `${stamp}-${Math.random().toString(16).slice(2, 8)}`
}

function stamp() {
  const now = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`
}

// 服务端回的是 UTC 的 ISO 时间，旧记录存的是本机时间字符串：都归到本机时间显示、按毫秒排序
const LEGACY_TIME = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$/

function parseTime(value) {
  if (!value) return null
  const legacy = LEGACY_TIME.exec(value)
  if (legacy) {
    const [, y, mo, d, h, mi, s] = legacy.map(Number)
    return new Date(y, mo - 1, d, h, mi, s)
  }
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

function displayTime(value) {
  const date = parseTime(value)
  if (!date) return value ? String(value) : ''
  const pad = (n) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

// 一份记录在界面上的样子：时间统一、原文地址按当前服务补全（别人分享的链接也得能取到原文）
function normalize(record) {
  if (!record || typeof record !== 'object') return null
  const docId = record.doc?.docId
  const date = parseTime(record.createdAt)
  return {
    ...record,
    // 原文只认 docId：存在哪台机器上都按「打开链接的这台服务」去取
    doc: docId
      ? {
        docId,
        sourceKind: record.doc?.sourceKind || 'pdf',
        pdfUrl: documentUrl(`/document/${docId}`),
      }
      : null,
    createdAt: displayTime(record.createdAt),
    at: date ? date.getTime() : 0,
    timings: record.timings || null,
    units: Array.isArray(record.units) ? record.units : [],
    blocks: Array.isArray(record.blocks) && record.blocks.length ? record.blocks : null,
    highlights: Array.isArray(record.highlights) && record.highlights.length ? record.highlights : null,
    ignored: Array.isArray(record.ignored) && record.ignored.length ? record.ignored : null,
  }
}

// 列表只要摘要：全文那几坨（units / blocks / highlights）留着占地方，进结果页时再取
function summaryOf(record) {
  const { units, blocks, highlights, ...rest } = record
  return { ...rest, hasDoc: Boolean(record.doc) }
}

// ── 列表 ──

// 「纠错记录」页看到的清单：服务端那份 + 本机还没进库的那些（离线存的、还没同步的）
export const records = computed(() => {
  const known = new Set(remote.value.map((row) => row.id))
  const pending = local.value
    .filter((row) => !known.has(row.id))
    .map((row) => normalize(row))
    .filter(Boolean)
    .map((row) => summaryOf(row))
  return [...remote.value, ...pending].sort((a, b) => (b.at || 0) - (a.at || 0))
})

async function loadRemote() {
  const data = await listRecords({ limit: 200 })
  return (data?.records || []).map((row) => normalize(row)).filter(Boolean)
}

export async function loadRecords() {
  try {
    remote.value = await loadRemote()
    storage.value = 'server'
  } catch {
    // 后端没配库（503）或者服务不通：退回本机浏览器里那份，页面照样能用
    remote.value = []
    storage.value = 'local'
  }
  return records.value
}

// 浏览器里攒下的旧记录一次性导进库（按 id 覆盖，重复导也没事）
async function syncLocal() {
  if (storage.value !== 'server' || !local.value.length) return
  let synced = 0
  for (const item of [...local.value]) {
    try {
      await createRecord(toPayload(item))
      if (item.ignored?.length) await putIgnored(item.id, item.ignored)
    } catch {
      // 服务端还是存不上：留着，下次启动再试
      break
    }
    local.value = local.value.filter((row) => row.id !== item.id)
    synced += 1
  }
  if (synced) persistLocal()
}

// 启动时把列表拉回来，顺手把旧记录导进库
export async function initRecords() {
  await loadRecords()
  await syncLocal()
  return records.value
}

// ── 单条记录 ──

// 结果页要的整份记录：先问服务端，服务端没有（或不通）就找本机那份
export async function getResult(id) {
  const key = String(id || '')
  if (!key) return null
  if (storage.value !== 'local') {
    try {
      const data = await fetchRecord(key)
      const record = normalize(data?.record)
      if (record) {
        cache.set(key, record)
        return record
      }
    } catch {
      // 服务端没有这条 / 服务不通：接着往下找本机那份
    }
  }
  const found = local.value.find((row) => row.id === key)
  const record = found ? normalize(found) : null
  if (record) cache.set(key, record)
  return record
}

// 存一条纠错记录：先进库；后端没配库或服务不通才退回本机浏览器（链接就只在这台机器上能开）
export async function saveResult(payload) {
  const body = toPayload({ ...payload, id: newLocalId() })
  try {
    const saved = await createRecord(body)
    storage.value = 'server'
    // 记录列表顺手刷新一下：不 await，失败也不影响跳结果页
    loadRecords()
    return { id: saved.id, stored: 'server' }
  } catch (err) {
    // 参数不对（400）是前端自己的问题，照实报出来；其余情况（503、服务不通）才退回本地
    if (err?.status && err.status >= 400 && err.status < 500 && err.status !== 503) throw err
    const record = normalize({ ...body, createdAt: stamp() })
    local.value = [record, ...local.value].slice(0, MAX_LOCAL)
    persistLocal()
    storage.value = 'local'
    return { id: record.id, stored: 'local' }
  }
}

function toPayload(record) {
  const doc = record.doc?.docId ? { docId: record.doc.docId, sourceKind: record.doc.sourceKind } : null
  return {
    id: record.id,
    kind: record.kind || 'file',
    filename: record.filename || '',
    engineStatus: record.engineStatus || 'ok',
    correctionCount: record.correctionCount || 0,
    unitsWithCorrection: record.unitsWithCorrection || 0,
    timings: record.timings || null,
    units: record.units || [],
    blocks: record.blocks || null,
    highlights: record.highlights || null,
    doc,
  }
}

function normalizeKeys(keys) {
  return [...new Set((keys || []).filter((key) => Number.isInteger(key)))].sort((a, b) => a - b)
}

// 误报的那些改动可以手动忽略：记在记录自身上（跟着记录走，换台机器打开也还在）。
// 传空数组就是全部恢复。这条记录只在本地时改本地那份，在服务端就改服务端的。
export async function setIgnored(id, keys) {
  const next = normalizeKeys(keys)
  const at = local.value.findIndex((row) => row.id === id)
  if (at >= 0) {
    local.value[at] = { ...local.value[at], ignored: next.length ? next : null }
    persistLocal()
  } else {
    // 存不上就抛出去，界面上能看到，不装作存上了
    await putIgnored(id, next)
  }
  const current = cache.get(id)
  if (!current) return null
  const updated = { ...current, ignored: next.length ? next : null }
  cache.set(id, updated)
  return updated
}

export async function removeResult(id) {
  const at = local.value.findIndex((row) => row.id === id)
  if (at >= 0) {
    local.value = local.value.filter((row) => row.id !== id)
    persistLocal()
    return
  }
  await deleteRecord(id)
  remote.value = remote.value.filter((row) => row.id !== id)
  cache.delete(id)
}

export function resultUrl(id) {
  // hash 路由：刷新、复制、前进后退都能还原
  return `${location.origin}${location.pathname}#/result/${id}`
}

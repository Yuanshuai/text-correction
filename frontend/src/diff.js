// 结果页的标记与定位：跟后端 correction/utils/text.py 用同一套重放规则
// （merge_adjacent_errors），把「有错段落」摊成一个个改动位置，再对到全文上。

// 结果页清单上的类型标签
export const TAG_LABEL = {
  replace: '改字',
  delete: '删字',
  insert: '增字',
}

function unescapeChar(value) {
  if (value === '␣') return ' '
  if (value === '\\n') return '\n'
  if (value === '\\t') return '\t'
  return value ?? ''
}

// 归一化后端 errors：兼容转义占位符（␣ / \n / \t）与非整数 position。
function normalizeErrors(errors) {
  const out = []
  for (const item of errors || []) {
    const position = Number(item?.position)
    if (!Number.isInteger(position)) continue
    out.push({
      position,
      original_char: unescapeChar(item?.original_char),
      correct_char: unescapeChar(item?.correct_char),
      tag: item?.tag,
    })
  }
  return out
}

// 把相邻的同类改动收成一处，规则跟后端 correction/utils/text.py 的 merge_adjacent_errors 完全一致：
// 连续删几个字算一处、连续改几个字算一处、同一位置补几条算一处；类型不同、或中间隔着没改的字，就分成两处。
// 结果页、纠错记录、页面上的「处数」都按这个口径数，数字才处处一致。
export function mergeAdjacentErrors(errors) {
  const ordered = normalizeErrors(errors).sort(
    (a, b) => a.position - b.position || String(a.tag).localeCompare(String(b.tag)),
  )
  const merged = []
  for (const err of ordered) {
    const { tag, position } = err
    if (!['replace', 'delete', 'insert'].includes(tag) || !Number.isInteger(position)) {
      merged.push({ ...err })
      continue
    }
    const previous = merged.length ? merged[merged.length - 1] : null
    if (previous && previous.tag === tag && Number.isInteger(previous.position)) {
      if (tag === 'insert' && previous.position === position) {
        previous.correct_char = (previous.correct_char || '') + (err.correct_char || '')
        continue
      }
      const previousSource = previous.original_char || ''
      if (tag !== 'insert' && previous.position + previousSource.length === position) {
        previous.original_char = previousSource + (err.original_char || '')
        previous.correct_char = (previous.correct_char || '') + (err.correct_char || '')
        continue
      }
    }
    merged.push({ ...err })
  }
  return merged
}

// 一段原文的改动摊平成「位置 + 吃掉的字数」：at 是原文里的下标，len 是这段改动占的原文长度
// （补字不吃原文，len = 0）。多字改动（「直到」->「指导」）要占满整段，只标第一个字看着像只改了一个字。
// 对不上原文的改动一律丢掉——宁可不标，也不能把没写错的字标成错的。
export function buildUnitMarks(text, errors, baseIndex = 0) {
  const source = text || ''
  const marks = []
  let next = baseIndex
  for (const err of mergeAdjacentErrors(errors)) {
    const at = err.position
    if (!Number.isInteger(at) || at < 0 || at > source.length) continue

    if (err.tag === 'insert') {
      if (!err.correct_char) continue
      marks.push({ idx: next, tag: 'insert', at, len: 0, source: '', target: err.correct_char })
      next += 1
      continue
    }
    if (err.tag !== 'replace' && err.tag !== 'delete') continue

    const original = err.original_char || ''
    if (!original || at >= source.length || source.slice(at, at + original.length) !== original) continue
    marks.push({
      idx: next,
      tag: err.tag,
      at,
      len: original.length,
      source: original,
      target: err.correct_char || '',
    })
    next += 1
  }
  return { marks, next }
}

// 按位置把一段文本切成 token：改动的字只上色（改=琥珀、删=红、补=绿），不上底色不划线。
export function renderBlock(text, marks) {
  const source = text || ''
  const ordered = [...(marks || [])].sort((a, b) => a.at - b.at || a.len - b.len)
  const tokens = []
  const push = (char, cls, idx) => tokens.push({ char, cls, idx })
  let cursor = 0

  for (const mark of ordered) {
    const at = Math.min(Math.max(mark.at, cursor), source.length)
    while (cursor < at) {
      push(source[cursor], '', -1)
      cursor += 1
    }
    if (mark.tag === 'insert') {
      for (const char of mark.target || '') push(char, 'mk-ins', mark.idx)
      continue
    }
    const cls = mark.tag === 'replace' ? 'mk-rep' : 'mk-del'
    for (let i = 0; i < Math.max(mark.len, 0) && cursor < source.length; i += 1) {
      push(source[cursor], cls, mark.idx)
      cursor += 1
    }
  }
  while (cursor < source.length) {
    push(source[cursor], '', -1)
    cursor += 1
  }
  return tokens
}
// 结果页：把后端给的全文分段（每段自带 errors）渲染成带标记的正文 + 右侧清单。
// 位置全部来自后端自己的解析结果，前端不用再猜对位。
//
// 表格的纠错是「一个单元格一个单元」，展示时要拼回一张表：每个单元格自带
// table（第几张表）/ row / colspan / rowspan，同一张表、同一行的格子按顺序归到一行。
export function buildDocView(blocks) {
  const view = []
  const marks = []

  const build = (block, index) => {
    const text = block?.text || ''
    const built = buildUnitMarks(text, block.errors, marks.length)
    for (const mark of built.marks) {
      // at / len / text 是给右侧报告截「改动所在的那句原话」用的：报告里直接读到原句，
      // 省得来回跳。text 是同一份字符串的引用，不多占内存。
      marks.push({
        idx: mark.idx,
        tag: mark.tag,
        source: mark.source,
        target: mark.target,
        at: mark.at,
        len: mark.len,
        text,
      })
    }
    return {
      key: index,
      text,
      count: built.marks.length,
      // 没错的内容按纯文本渲染，不切成一个个 span——长文档省一大截 DOM
      tokens: built.marks.length ? renderBlock(text, built.marks) : null,
      colspan: Math.max(Number(block?.colspan) || 1, 1),
      rowspan: Math.max(Number(block?.rowspan) || 1, 1),
    }
  }

  let table = null
  ;(blocks || []).forEach((block, index) => {
    if (block?.type === 'row' && Number.isInteger(block.table)) {
      if (!table || table.table !== block.table) {
        table = { key: index, kind: 'table', table: block.table, rows: [], count: 0, cols: 0 }
        view.push(table)
      }
      const cell = build(block, index)
      const row = table.rows[block.row] || (table.rows[block.row] = { cells: [], width: 0 })
      // 表格解析偶尔会漏掉开头的格子：补几个空格子，免得整行往左错位
      const col = Number(block.col) || 0
      if (col > row.width) {
        row.cells.push({ key: `${index}-gap`, text: '', count: 0, tokens: null, colspan: col - row.width, rowspan: 1 })
      }
      row.cells.push(cell)
      row.width = Math.max(col, row.width) + cell.colspan
      table.cols = Math.max(table.cols, row.width)
      table.count += cell.count
      return
    }

    table = null
    const built = build(block, index)
    if (!built.text.trim() && !built.count) return
    view.push({ ...built, kind: 'p', type: block?.type })
  })

  return {
    blocks: view,
    marks,
  }
}

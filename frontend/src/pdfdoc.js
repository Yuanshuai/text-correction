// PDF.js 的薄封装：结果页要「跟直接打开原文件一个样」，所以直接把 PDF 一页页画到画布上，
// 高亮框再按后端给的坐标贴上去——不重排文字，就不会跟原文件长得不一样。
//
// 这里单独一个文件是为了让 PDF.js（连同它的 worker，好几 MB）只在真打开结果页时才加载：
// 首屏只有上传界面，不需要背着阅读器。
import * as pdfjs from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl

// CMaps / 标准字体放在产物根目录（构建时从 node_modules 拷过来，见 vite.config.js）。
// 用 baseURI 相对定位，网关把页面挂到哪个子路径下都能取到。
function asset(name) {
  return new URL(`${name}/`, document.baseURI).href
}

export async function loadDocument(url) {
  return pdfjs.getDocument({
    url,
    cMapUrl: asset('cmaps'),
    cMapPacked: true,
    standardFontDataUrl: asset('standard_fonts'),
    isEvalSupported: false,
  }).promise
}

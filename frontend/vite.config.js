import { cpSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// PDF 阅读器要的辅助资源（CJK 的 CMap、标准字体）不会被自动打进产物，
// 缺了它们遇到 CID 字体或非内嵌字体的 PDF 会整页空白，所以构建完顺手拷进 dist。
function copyPdfjsAssets() {
  return {
    name: 'copy-pdfjs-assets',
    closeBundle() {
      const from = fileURLToPath(new URL('./node_modules/pdfjs-dist/', import.meta.url))
      const to = fileURLToPath(new URL('./dist/', import.meta.url))
      cpSync(`${from}cmaps`, `${to}cmaps`, { recursive: true })
      cpSync(`${from}standard_fonts`, `${to}standard_fonts`, { recursive: true })
    },
  }
}

// 开发时把 /api 反代到纠错服务；可用 CORRECTION_API 覆盖目标地址。
const target = process.env.CORRECTION_API || 'http://127.0.0.1:32546'
const proxy = {
  '/api': {
    target,
    changeOrigin: true,
    rewrite: (path) => path.replace(/^\/api/, ''),
  },
}

export default defineConfig({
  plugins: [vue(), copyPdfjsAssets()],
  server: {
    host: true,
    port: 5173,
    proxy,
  },
  // 本地预览生产产物时走同一套反代，跟容器里 nginx 的 location /api/ 对齐，
  // 这样 `npm run build && npm run preview` 就能在发布前自检一次。
  preview: {
    host: true,
    port: 4173,
    proxy,
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})

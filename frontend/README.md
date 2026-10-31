# text-correction-web

文本纠错服务的**前端**：Vue 3 + Vite + PDF.js，无 UI 组件库依赖。**单独一个镜像**，跟后端分离——
只通过 HTTP 调后端的接口，前后端可以各自发布。结果页把原文件一页页画出来，改动直接框在原字上。

## 功能

- **文本纠错**：粘贴文本调 `POST /correction_text`，纠完直接打开结果页。
- **文档纠错**：上传 PDF / Word / Excel / PPT 调 `POST /correction`，支持「处理段落上限」；纠错期间留在当前页显示已用时间，
  完成后**自动打开结果页**（可关）。**不开空白页干等**。
- **结果页（PDF 阅读器 + 高亮框）**：地址形如 `#/result/<id>`。左侧拿后端托管的 PDF（`GET /document/<id>`）用
  PDF.js 一页页画出来，**跟直接打开原文件一个样**；改动按后端给的坐标贴成框，落在原来那几个字上。
  Word / Excel / PPT 也一样：后端先把它们转成 PDF，之后跟 PDF 完全同一条链路。
  右侧是纠错清单，只有「类型 + 原文 → 改后」（深色加粗），两边悬停互亮、点击互跳。
  页头不放处数/时间，清单里不放序号和段号——这些没人看。
- **兜底渲染**：后端没托管 PDF 时（没配转换服务、或老记录）自动退回「把文字重排出来、在字上标色」那套，
  表格照原样拼回一张表（合并单元格也还原，列多的宽表给足最小列宽、就地横向滚动），功能不丢。
- **上/下一个错字**：右上角 `‹ ›` 或者键盘 `←` `→`（`↑` `↓` 同义）直接跳到上/下一处，循环、自动居中。
- **纠错记录**：列出最近的结果，可打开、可删除。
- **服务状态**：右上角徽标调 `GET /health/correction`，一眼看出纠错链路是否真的可用
  （`tool_calls=0` 说明模型路由没在调工具，纠错会静默为空）。

## 标记样式

只标**具体改到的字**，不上底色、不划横线。PDF 阅读器那套把改动框在原字上（同一行的相邻字合成一个框，
多字改动是一个整框；当前那一处再圈一道蓝）：

| 类型 | PDF 上的框 | 文字重排（兜底） |
|------|-----------|-----------------|
| 改字 | 琥珀底 + 琥珀边框 | 琥珀色加粗 |
| 删字 | 红底 + 红边框（**不用删除线**） | 红色加粗 |
| 增字 | 一根绿色细竖线（插在锚点处） | 绿色加粗，插在锚点字前面一起显示 |

「处」的口径：连续的同类型改动合并成一处（连着删几个字算一处、同一位置补几条算一处），
`src/diff.js` 的 `mergeAdjacentErrors` 就是这套规则；对不上原文的改动一律不标。

## 结果与记录存在哪

存在**后端 MySQL** 里（`MYSQL_DSN`，表 `correction_record`）：全文分段、改动清单、改动贴在页面哪儿、
耗时、以及被忽略的误报。所以同一个 `#/result/<id>` 链接换台机器、换个浏览器都打得开，也分享得出去。
原文 PDF 仍由后端缓存/托管，库里只存文档 id（几十页的 PDF 塞进库只会把库撑大）。

后端没配库（或库连不上）时，记录接口回 503，前端自动退回浏览器本地存储（`localStorage`）：
功能不丢，只是链接不通用。库里能连上之后，浏览器里攒下的旧记录会在下次进页面时一次性导进去
（按 id 覆盖，所以库里存的 id 跟原来的链接对得上）。

原文件由后端留在 `DOC_CACHE_DIR`：没有记录引用的只保留 `DOC_TTL_HOURS=24` 小时，
**被记录引用的那份跟着记录一起留着**（记录删掉之后才按 TTL 清），否则分享出去的链接就只有报告没有原文。

## 开发

```bash
cd frontend
npm ci               # 依赖按 package-lock.json 装，保证一致
npm run dev          # http://localhost:5173
```

开发态 `/api` 走 vite 代理（见 `vite.config.js`），默认转发到 `http://127.0.0.1:32546`，可用环境变量覆盖：

```bash
CORRECTION_API=http://10.0.0.10:32546 npm run dev
```

要在本地看 Word/Excel 转 PDF 那套结果页，后端得先配好转换服务（`CONVERT_SERVER`）。本机可以直接起一个：

```bash
docker run -d --name tcw-gotenberg -p 3010:3000 gotenberg/gotenberg:8 gotenberg --api-timeout=120s
# 然后后端 .env 里写 CONVERT_SERVER=http://127.0.0.1:3010
```

## 构建

```bash
npm run build        # 产物在 dist/
npm run preview      # 用同一套 /api 反代预览产物，发布前自检
```

## 镜像（独立部署）

```bash
# 构建：上下文就是这个目录（仓库根的 Dockerfile 是后端的，跟这个无关）
docker build -t text-correction-web:latest ./frontend

# 运行：容器的 80 端口对外，/api 反代到后端
docker run -d --name text-correction-web -p 8080:80 \
  -e CORRECTION_UPSTREAM=http://text-correction:32546 \
  text-correction-web:latest
```

**构建期参数**

| 参数 | 默认 | 说明 |
|------|------|------|
| `VITE_API_BASE` | `/api` | 前端去哪儿找后端。同源反代就用默认；前后端不同源且不打算反代时，填后端完整地址 |

**运行期环境变量**

| 变量 | 默认 | 说明 |
|------|------|------|
| `CORRECTION_UPSTREAM` | `http://text-correction:32546` | nginx 把 `/api/*` 反代到这儿（去掉 `/api` 前缀），`/openapi/*` 原样透传 |

几点说明：

- 镜像 = Vite 构建产物 + nginx：依赖由 `package-lock.json` 锁死（`npm ci`），`node_modules` / `dist` / 本地 `.env.*.local` 都不进镜像（见 `.dockerignore`）。
- nginx 用官方镜像的模板机制（`/etc/nginx/templates/default.conf.template`），启动时把 `${CORRECTION_UPSTREAM}` 替换进配置再启用。
- `/api` 反代是**同源**的，所以浏览器侧不存在跨域问题；后端服务没有 CORS 中间件，走直连会失败。对外接口也从同一个端口进（`/openapi/contract-audit/web-correct`），整套服务只需要这一个端口对外。
- 整份文档纠错可能要几分钟，反代的读写超时都放宽到 300s，上传上限 100m。
- 用 `/correction`（带 `full=true` 取全文分段、高亮框和 PDF 地址）、`/document/<id>`、`/correction_text`、`/health/correction` 四个接口。
- PDF.js 的 worker 是 `.mjs`：nginx 自带的 `mime.types` 不认这个后缀，会被当成 octet-stream，
  浏览器按模块脚本的严格 MIME 校验直接拒掉（结果页只剩「Setting up fake worker failed」）。
  `nginx.conf` 里因此专门补了一条 `location ~* \.mjs$ { default_type application/javascript; }`；
  若静态文件由外面的网关托管，也要保证 `.mjs` 回 `application/javascript`。
- PDF.js 的 CMap / 标准字体在构建时从 `node_modules/pdfjs-dist` 拷进产物（`vite.config.js` 里的
  `copyPdfjsAssets`），缺了它们遇到 CID 字体或非内嵌字体的 PDF 会整页空白。
- CI 里前端镜像是独立的 tag（`:standalone-web-x86` / `:standalone-web-arm64`，跟后端同一个仓库），跟后端镜像分别构建、分别推送；我们自己的 tag 前缀是 `standalone-`，客户那边的 `legacy-*` / `legacy-*` 不受影响。

## 目录

```
src/
  api.js                      接口封装（/correction_text、/correction、/document/<id>、/records、/health/correction）
  diff.js                     改动位置（相邻同类改动合并）+ 兜底渲染（全文分段 -> 带标记的 token）
  pdfdoc.js                   PDF.js 薄封装（结果页的 PDF 阅读器用它加载文档）
  results.js                  纠错结果与记录（走后端 MySQL，连不上时退回 localStorage）
  App.vue                     外壳：hash 路由 + 标签页 + 服务状态
  components/
    HealthBadge.vue           服务状态徽标
    TextCorrection.vue        文本纠错（提交即纠错、自动打开结果页）
    FileCorrection.vue        文档纠错（上传即纠错、自动打开结果页）
    ResultPage.vue            结果页（#/result/<id>：PDF 阅读器 + 高亮框，兜底文字重排）
    HistoryPanel.vue          纠错记录
```

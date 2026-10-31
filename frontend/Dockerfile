# 纠错前端（text-correction-web）独立镜像：跟后端镜像（仓库根 Dockerfile）没有任何关系。
# 构建上下文就是这个目录：
#   docker build -t text-correction-web:latest ./frontend
#   docker run -d -p 8080:80 --add-host text-correction:host-gateway \
#     -e CORRECTION_UPSTREAM=http://text-correction:32546 text-correction-web:latest
FROM node:20-alpine AS build

WORKDIR /app

# 依赖只认锁文件：先拷这两个，之后改源码不会重复装一遍依赖
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .

# 前端去哪儿找后端：默认同源 /api，由运行时的 nginx 反代到纠错服务。
# 前后端不同源、也不打算反代时，构建期覆盖：
#   --build-arg VITE_API_BASE=https://correction.example.com
ARG VITE_API_BASE=/api
ENV VITE_API_BASE=${VITE_API_BASE}
RUN npm run build

FROM nginx:1.27-alpine

LABEL org.opencontainers.image.title="text-correction-web" \
      org.opencontainers.image.description="文本纠错服务的前端（Vue 3 构建产物 + nginx）"

COPY --from=build /app/dist /usr/share/nginx/html
# nginx 官方镜像启动时会把 templates/*.template 里的 ${VAR} 换掉再放进 conf.d
COPY nginx.conf /etc/nginx/templates/default.conf.template

# /api 反代目标；不指定时指向同一容器网络里的后端
ENV CORRECTION_UPSTREAM=http://text-correction:32546

# 反代等后端回话最多多久：得比「等外部解析结果」（PARSE2JSON_MAX_WAIT_MIN，默认 60 分钟）
# 加纠错那段时间更宽裕，否则大文档会被 nginx 掐成 504。默认 2 小时，compose 里可用
# PROXY_TIMEOUT 覆盖（这里给默认值，免得少了这个变量 nginx 起不来）。
ENV PROXY_TIMEOUT=7200s

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1

#!/usr/bin/env bash
# 纠错服务 · 一键安装（服务器上跑这个）
#
#   ./install.sh
#
# 依次做：查 Docker → 查配置 → 装载镜像 → 起容器 → 等健康检查 → 打印访问地址。
# 包是离线可用的：镜像就在本目录的 images/ 里，不从网上拉。只有 images/ 里没有的
# 镜像（用 --minimal 打的包）才会尝试联网拉取。
set -eo pipefail

cd "$(dirname "$0")"

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; DIM=$'\033[2m'; RST=$'\033[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✔%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s!%s %s\n' "$YEL" "$RST" "$*"; }
die()  { printf '%s✘%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }

# ── 1. Docker ──
command -v docker >/dev/null 2>&1 || die "没找到 docker，先装 Docker：https://docs.docker.com/engine/install/"
docker info >/dev/null 2>&1 || die "docker 命令用不了（守护进程没起，或当前账号没有权限，可试 sudo）"
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  die "没找到 compose 插件，先装：https://docs.docker.com/compose/install/linux/"
fi
ok "Docker 就绪（$("${COMPOSE[@]}" version --short 2>/dev/null | head -n1)）"

# ── 2. 配置 ──
val() { sed -n "s/^[[:space:]]*$1=//p" .env | tail -n1 | sed 's/^["'\'']//; s/["'\'']$//'; }

if [ ! -f .env ]; then
  cp .env.example .env
  warn "已按模板生成 .env。打开它填好下面几项，再重新执行一次本脚本："
  say  "    ${DIM}OPEN_BASE_URL      模型网关地址${RST}"
  say  "    ${DIM}OPEN_AI_MODEL      模型名${RST}"
  say  "    ${DIM}OPEN_AI_TOKEN      调用模型的 Bearer token${RST}"
  exit 1
fi

MISSING=""
need() { [ -n "$(val "$1")" ] || MISSING="$MISSING $1"; }
need OPEN_BASE_URL
need OPEN_AI_MODEL
need OPEN_AI_TOKEN
[ -z "$MISSING" ] || die ".env 里这几项还是空的：$MISSING"
ok ".env 配置齐全"

# ── 3. 镜像 ──
if ls images/*.tar >/dev/null 2>&1; then
  for f in images/*.tar; do
    say "装载镜像 $(basename "$f") …"
    docker load -i "$f" >/dev/null || die "$(basename "$f") 装载失败"
  done
  ok "包里的镜像都装载了"
else
  warn "包里没有 images/，改为从镜像仓库拉（需要这台服务器能出网）"
fi

# 我们自己那两个镜像：.env 里指了镜像仓库就用仓库地址，否则用本机（或离线包里）的同名镜像
CORR_REF=$(val CORRECTION_IMAGE); [ -n "$CORR_REF" ] || CORR_REF=text-correction:latest
CORR_WEB_REF=$(val CORRECTION_WEB_IMAGE); [ -n "$CORR_WEB_REF" ] || CORR_WEB_REF=text-correction-web:latest
# 三个官方镜像也可能被 .env 换到国内镜像源了（见 .env.example）
MYSQL_REF=$(val MYSQL_IMAGE); [ -n "$MYSQL_REF" ] || MYSQL_REF=mysql:8.0
MINIO_REF=$(val MINIO_IMAGE); [ -n "$MINIO_REF" ] || MINIO_REF=minio/minio:latest
GOTENBERG_REF=$(val GOTENBERG_IMAGE); [ -n "$GOTENBERG_REF" ] || GOTENBERG_REF=gotenberg/gotenberg:8
REQUIRED=("$CORR_REF" "$CORR_WEB_REF" "$MYSQL_REF" "$MINIO_REF" "$GOTENBERG_REF")
NEED_PULL=""
for ref in "${REQUIRED[@]}"; do
  docker image inspect "$ref" >/dev/null 2>&1 || NEED_PULL="$NEED_PULL $ref"
done
if [ -n "$NEED_PULL" ]; then
  warn "本机缺这几个镜像，尝试联网拉取：$NEED_PULL"
  for ref in $NEED_PULL; do
    docker pull "$ref" || die "拉不到 $ref —— 服务器不通外网的话，请改用带官方镜像的完整包"
  done
fi
ok "五个镜像就绪"
if ls images/*.tar >/dev/null 2>&1 && { [ "$CORR_REF" != "text-correction:latest" ] || [ "$CORR_WEB_REF" != "text-correction-web:latest" ]; }; then
  warn "包里的镜像装好了，但 .env 把镜像指到了仓库地址，compose 会去仓库拉；想用包里的就把 CORRECTION_IMAGE / CORRECTION_WEB_IMAGE 留空"
fi

# ── 4. 起服务 ──
"${COMPOSE[@]}" up -d

wait_healthy() {
  local svc=$1 timeout=$2 waited=0 id status
  while [ "$waited" -lt "$timeout" ]; do
    id=$("${COMPOSE[@]}" ps -q "$svc" 2>/dev/null || true)
    if [ -n "$id" ]; then
      status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null || echo "")
      if [ "$status" = "healthy" ]; then ok "$svc 就绪"; return 0; fi
      if [ "$status" = "unhealthy" ]; then
        "${COMPOSE[@]}" logs --tail 30 "$svc" >&2
        die "$svc 起不来，日志见上"
      fi
    fi
    sleep 3; waited=$((waited + 3))
  done
  "${COMPOSE[@]}" logs --tail 30 "$svc" >&2
  die "$svc 等了 ${timeout} 秒还没就绪，日志见上"
}

wait_healthy mysql 240
wait_healthy minio 240
wait_healthy correction 300
wait_healthy web 120

# ── 5. 收工 ──
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -n "$IP" ] || IP=127.0.0.1
PORT=$(val FRONTEND_PORT); [ -n "$PORT" ] || PORT=18080

say ""
ok "纠错服务已经跑起来了"
say "  前端页面 / 对外接口  http://${IP}:${PORT}"
say "  ${DIM}后端、数据库、对象存储都只在 compose 网络里互访，不对外开端口${RST}"
say ""
say "  自检（模型那条路由要支持工具调用，不然纠错会静默为空）："
say "    curl -s http://127.0.0.1:${PORT}/api/health"
say "    curl -s http://127.0.0.1:${PORT}/api/health/correction   ${DIM}# 深度自检，花 token：要 status=ok 且 tool_calls>0${RST}"
say ""
say "  常用（都在本目录下）："
say "    ${DIM}${COMPOSE[*]} logs -f correction${RST}     看后端日志"
say "    ${DIM}${COMPOSE[*]} restart correction${RST}     重启后端"
say "    ${DIM}${COMPOSE[*]} down${RST}                   停止（数据保留）"
DATA_DIR=$(val MYSQL_DATA_DIR)
if [ -n "$DATA_DIR" ]; then
  say "    ${DIM}数据在 ${DATA_DIR%/*}/${RST}              ${DIM}备份/搬走就是打包这个目录${RST}"
else
  say "    ${DIM}${COMPOSE[*]} down -v${RST}                停止并清空数据（命名卷）"
fi
say ""
warn "接口本身没有登录鉴权，只放内网；要对公网就把认证挡在网关那一层。"

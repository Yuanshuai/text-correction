# 纠错服务 · 部署

## 如果这台服务器能出网：其实不用这个包

整套部署就是「一个 `docker-compose.yml` + 一个 `.env`」两个文件：

```bash
docker login <镜像仓库>                      # 私有仓库才需要
docker compose pull && docker compose up -d
```

`.env` 里的 `CORRECTION_IMAGE` / `CORRECTION_WEB_IMAGE` 填镜像仓库里的地址（CI 推上去的那两个
tag，按机器架构选 `-x86` / `-arm64`）。升级就是换个 tag 再 pull + up，数据不动。

`mysql` / `minio` / `gotenberg` 三个官方镜像走 Docker Hub，那台机器拉不动（或很慢）时，在 `.env` 里
把它们指到国内镜像源里的同一份镜像就行，不用去改服务器上 docker 的配置：

```ini
MYSQL_IMAGE=docker.m.daocloud.io/library/mysql:8.0
MINIO_IMAGE=docker.m.daocloud.io/minio/minio:latest
GOTENBERG_IMAGE=docker.m.daocloud.io/gotenberg/gotenberg:8
```

只有 `ghcr.io` 那个私有仓库不受镜像源影响，照旧登录后拉。

## 如果这台服务器不出网：用这个包

1. 服务器先有 Docker（`docker --version`）和 compose 插件（`docker compose version`）。
2. 把包传上去，解压：

   ```bash
   tar xzf text-correction-*.tar.gz
   cd text-correction-*/
   ```

3. 跑安装脚本：

   ```bash
   ./install.sh
   ```

   第一次跑它会按模板生成 `.env`，并告诉你该填哪几项；填好之后再跑一次，它就一路把服务起起来。
4. 跑完会打印访问地址，浏览器打开 `http://<服务器IP>:18080` 就能用了。

## 包里是什么

- `docker-compose.yml`：五个容器 —— 前端、后端、原文对象存储（MinIO）、纠错记录库（MySQL）、文档转换（Gotenberg）
- `images/`：镜像文件，安装脚本会自动装载（离线，不用联网拉）
- `.env.example`：配置模板
- `install.sh`：一键安装脚本

## 端口

| 端口 | 是什么 | 要不要对外 |
| --- | --- | --- |
| 18080 | 前端页面 + 对外接口（用户和外部系统都走这一个口）；端口就是 `.env` 里的 `FRONTEND_PORT`，被占了就换，比如 `18081` | 要 |

后端（32546）、对象存储（9000 / 9001）、数据库、文档转换都只在 compose 网络里按服务名互访，**不发布到宿主机**。
想在服务器上直接看后端或对象存储控制台：`docker compose exec correction curl -s localhost:32546/health`；
临时要开也行，但建议只绑 `127.0.0.1`（`ports: ["127.0.0.1:32546:32546"]`）。

## 常用命令（都在解压出来的目录里执行）

```bash
docker compose ps                    # 看状态
docker compose logs -f correction    # 看后端日志
docker compose restart correction    # 重启后端
docker compose down                  # 停止（数据保留）
docker compose down -v               # 停止并清空数据，慎用
```

## 部署目录长什么样

服务器上就一个目录，所有东西都放在里面（例如 `/data/text-correction`）：

```
/data/text-correction/
├── docker-compose.yml    五个容器的编排
├── .env                  配置：镜像、模型、端口、口令、数据目录
├── images/               三个官方镜像的 tar（离线装机用，装完可以删；联网拉的话不用传）
└── data/                 运行期数据（mysql/ + minio/），备份就是打包这个目录
```

## 数据在哪儿

默认落在 Docker 命名卷里（`<项目名>_mysql-data`、`<项目名>_minio-data`）。想让数据跟着部署目录走
（备份就是打包目录、换机器整个目录拷走），在 `.env` 里指两个目录：

```ini
MYSQL_DATA_DIR=./data/mysql
MINIO_DATA_DIR=./data/minio
```

`./data` 是相对部署目录的路径，起容器时自动建。两种方式只是位置不同：命名卷下 `down -v` 会清空，
目录下 `down -v` 不会动它（要清就自己删 `data/`）。

## 换模型 / 调参数

改 `.env`，然后 `docker compose up -d`（会自动重建配置变了的容器；`restart` 不会重读配置）。
改完看 `docker compose logs correction | head`，启动那行会打出实际生效的模型和参数。

## 服务器在一台 Windows 里（要靠端口转发才进得来）

Docker 跑在 Linux 上、Linux 的地址只有宿主机（Windows）能到，那就在 **Windows 上**加一条转发，
让局域网的 `10.0.0.10:18081` 转到 Linux 的 `10.0.0.215:18081`（在 PowerShell 里执行，管理员）：

```powershell
netsh interface portproxy add v4tov4 listenaddress=10.0.0.10 listenport=18081 connectaddress=10.0.0.215 connectport=18081
netsh advfirewall firewall add rule name="tc-web-18081" dir=in action=allow protocol=TCP localport=18081
```

查已有的转发：`netsh interface portproxy show v4tov4`；撤销：把 `add` 换成 `delete` 再执行一遍。
Linux 这边不用额外动防火墙（那台机器上没有 ufw / firewalld），进不进得来全看上面这两条。

## 对外接口回的链接指向哪儿

`/openapi/contract-audit/web-correct` 回的 `url` 默认按这次请求的地址拼（外部系统调的是前端那个
端口，nginx 把 Host 原样透传过来，所以拼出来就是用户能打开的那个地址）。不想让它自动拼、要在
后面挂个域名或网关时，`.env` 里填 `PUBLIC_BASE_URL` 钉死。

## 生产环境（有公网 IP）多注意三件事

1. **接口没有登录鉴权**，公网开了端口就等于对全网开放。至少用云安全组 / 防火墙把来源限到调用方；
   更强一点就把认证放在网关那一层。只想给自己机器上的 nginx/网关转发，可以把 `web` 的
   ports 改成 `127.0.0.1:${FRONTEND_PORT}:80`，公网就直连不到了。
2. **`.env` 里把 `PUBLIC_BASE_URL` 钉成公网地址**（如 `http://203.0.113.10:18081`），
   这样调用方不管从内网还是外网进来，回的结果页链接都是同一个、外部用户打得开的地址。
   只服务内网用户时，钉内网地址（如 `http://10.0.0.6:18081`）。
3. **口令换成自己的**（`MYSQL_ROOT_PASSWORD` / `MYSQL_PASSWORD` / `S3_ACCESS_KEY` / `S3_SECRET_KEY`），
   第一次部署就填好；之后再改对已有的数据卷无效。

各环境的 `.env` 模板在 `deploy/test-env/.env`、`deploy/prod-env/.env`；compose 文件**只有一份**，就是
仓库根目录的 `docker-compose.yml`（`deploy/*/` 里不再放副本，免得改了根目录那份、传上去的却是旧副本）。

## 注意

- 接口本身没有登录鉴权，只放内网；要对公网，认证据挡在网关那一层。
- `.env` 里的数据库口令、对象存储口令建议第一次部署就改掉，之后再改对已有数据卷无效。

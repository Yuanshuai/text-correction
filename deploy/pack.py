"""把整套纠错服务打成一个离线安装包：服务器上解包、跑 install.sh 就行。

在仓库根目录执行：

    python deploy/pack.py                # 全套：连 mysql / minio / gotenberg 一起装进去，
                                         # 服务器完全不用出网
    python deploy/pack.py --minimal      # 只装我们自己的两个镜像，官方镜像让服务器自己拉
    python deploy/pack.py --skip-build   # 镜像已经构建好了，直接打包

产出：deploy/dist/text-correction-<版本>.tar.gz
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tarfile
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DIST = HERE / "dist"

# 我们自己的两个镜像：包里一定带（就是这两个 tar）
OUR_IMAGES = [
    ("text-correction:latest", "images/text-correction.tar"),
    ("text-correction-web:latest", "images/text-correction-web.tar"),
]
# 官方镜像：默认也打进去，这样服务器不用出网；--minimal 时省掉
OFFICIAL_IMAGES = [
    ("mysql:8.0", "images/mysql-8.0.tar"),
    ("minio/minio:latest", "images/minio-latest.tar"),
    ("gotenberg/gotenberg:8", "images/gotenberg-8.tar"),
]
BUILDS = [
    ("后端", ["docker", "build", "-t", "text-correction:latest", "."]),
    ("前端", ["docker", "build", "-t", "text-correction-web:latest", "./frontend"]),
]


def run(cmd: list) -> None:
    print("+ " + " ".join(str(part) for part in cmd), flush=True)
    subprocess.run([str(part) for part in cmd], cwd=ROOT, check=True)


def has_image(ref: str) -> bool:
    return subprocess.run(
        ["docker", "image", "inspect", ref], capture_output=True
    ).returncode == 0


def strip_build_lines(source: str) -> str:
    """去掉 compose 里的 build 那两行：服务器上只有镜像、没有源码。"""
    kept = [line for line in source.splitlines() if not line.startswith("    build:")]
    return "\n".join(kept) + "\n"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="打一个离线一键安装包")
    parser.add_argument("--version", default=datetime.now().strftime("%Y%m%d"),
                        help="包名里的版本号，默认今天")
    parser.add_argument("--minimal", action="store_true",
                        help="只带自己的两个镜像（服务器需能拉官方镜像）")
    parser.add_argument("--skip-build", action="store_true", help="不重新构建镜像")
    parser.add_argument("--no-compress", action="store_true", help="只摊开目录，不打成 tar.gz")
    args = parser.parse_args()

    images = OUR_IMAGES + ([] if args.minimal else OFFICIAL_IMAGES)
    name = f"text-correction-{args.version}"
    out = DIST / name

    if not args.skip_build:
        for label, cmd in BUILDS:
            print(f"构建{label}镜像…", flush=True)
            run(cmd)
    for ref, _ in OUR_IMAGES:
        if not has_image(ref):
            print(f"镜像 {ref} 不存在，先构建（去掉 --skip-build）或者手动 docker build", file=sys.stderr)
            return 1

    if out.exists():
        shutil.rmtree(out)
    (out / "images").mkdir(parents=True)

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    (out / "docker-compose.yml").write_text(strip_build_lines(compose), encoding="utf-8")
    shutil.copy2(ROOT / ".env.example", out / ".env.example")
    shutil.copy2(HERE / "README.md", out / "README.md")
    shutil.copy2(HERE / "install.sh", out / "install.sh")
    (out / "install.sh").chmod(0o755)

    for ref, rel in images:
        dest = out / rel
        print(f"导出镜像 {ref} …", flush=True)
        started = time.time()
        run(["docker", "save", "-o", str(dest), ref])
        print(f"    {dest.stat().st_size / 1e6:.0f} MB，用了 {time.time() - started:.0f} 秒", flush=True)

    if args.no_compress:
        print(f"\n完成：{out}")
        return 0

    tarball = DIST / f"{name}.tar.gz"
    print(f"压缩成 {tarball.name} …（几个 G 的镜像，耐心等一会儿）", flush=True)
    started = time.time()
    with tarfile.open(tarball, "w:gz") as archive:
        archive.add(out, arcname=name)
    print(f"\n完成：{tarball}")
    print(f"大小：{tarball.stat().st_size / 1e9:.2f} GB，压缩用了 {time.time() - started:.0f} 秒")
    print(f"sha256：{sha256_of(tarball)}")
    print("\n服务器上：tar xzf <包名> && cd " + name + " && ./install.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

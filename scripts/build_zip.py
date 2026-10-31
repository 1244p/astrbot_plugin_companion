"""把插件打包成 AstrBot 可以直接上传安装的 zip。

用法:
    python scripts/build_zip.py            # 输出 dist/astrbot_plugin_companion-vX.Y.Z.zip
    python scripts/build_zip.py --out D:/x.zip

包内结构是 `<插件目录名>/metadata.yaml ...`，AstrBot 的 install_plugin_from_file
会解压后剥掉这一层，因此必须保留顶层目录。
"""

from __future__ import annotations

import argparse
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR_NAME = os.path.basename(ROOT)

# 进包的东西：跟着插件真的会跑的文件。测试和截图工具不进包，省体积。
INCLUDE_DIRS = ("api", "core", "views", "scripts")
INCLUDE_FILES = (
    "main.py",
    "metadata.yaml",
    "_conf_schema.json",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "requirements.txt",
)
# scripts 里只有迁移脚本对用户有用，打包器自己不进包
EXCLUDE_FILES = {"build_zip.py", "preview_server.py"}
SKIP_DIRS = {"__pycache__", ".git", ".preview", "dist", "tests"}
SKIP_SUFFIX = (".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".log")


def version() -> str:
    path = os.path.join(ROOT, "metadata.yaml")
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip()
    return "unknown"


def collect() -> list[str]:
    """返回相对路径列表（相对插件根目录）。"""
    paths: list[str] = []

    def walk(rel_dir: str) -> None:
        abs_dir = os.path.join(ROOT, rel_dir)
        for name in sorted(os.listdir(abs_dir)):
            if name in SKIP_DIRS or name.endswith(SKIP_SUFFIX):
                continue
            rel = os.path.join(rel_dir, name).replace("\\", "/")
            abs_path = os.path.join(ROOT, rel)
            if os.path.isdir(abs_path):
                walk(rel)
            elif name not in EXCLUDE_FILES:
                paths.append(rel)

    for name in INCLUDE_FILES:
        if os.path.isfile(os.path.join(ROOT, name)):
            paths.append(name)
    for name in INCLUDE_DIRS:
        if os.path.isdir(os.path.join(ROOT, name)):
            walk(name)
    return paths


def build(out_path: str) -> str:
    paths = collect()
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in paths:
            abs_path = os.path.join(ROOT, rel)
            arcname = f"{PLUGIN_DIR_NAME}/{rel}"
            info = zipfile.ZipInfo.from_file(abs_path, arcname)
            # 固定时间戳让同样的内容打出同样的字节，方便比对
            info.date_time = (2026, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(abs_path, "rb") as f:
                z.writestr(info, f.read())
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打包 AstrBot 插件 zip")
    parser.add_argument("--out", default="", help="输出路径，默认 dist/<目录名>-<版本>.zip")
    args = parser.parse_args(argv)

    out = args.out or os.path.join(
        ROOT, "dist", f"{PLUGIN_DIR_NAME}-{version()}.zip"
    )
    path = build(out)
    size = os.path.getsize(path)
    files = collect()
    print(f"打包完成: {path}")
    print(f"版本: {version()}  文件: {len(files)} 个  大小: {size / 1024:.1f} KB")
    for rel in files:
        print(f"  {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

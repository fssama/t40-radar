#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 Termux 镜像递归下载某个包的 .deb 及全部依赖，供离线安装。

用法：
    python tools/fetch_termux_pkgs.py python
    python tools/fetch_termux_pkgs.py python --out _tmp/termux-debs
    python tools/fetch_termux_pkgs.py python --mirror https://packages-cf.termux.dev

下载完成后，把整个目录推到手机：
    adb push _tmp/termux-debs /sdcard/Download/termux-debs

再在 Termux 里：
    cd ~/storage/downloads/termux-debs
    apt install ./*.deb
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.request
import lzma
import io

DEFAULT_MIRRORS = [
    "https://mirrors.ustc.edu.cn/termux/apt/termux-main",
    "https://packages-cf.termux.dev/apt/termux-main",
    "https://mirrors.bfsu.edu.cn/termux/apt/termux-main",
]

ARCH = "aarch64"
UA = {"User-Agent": "Termux-Offline-Fetcher/1.0"}


def fetch(url: str, timeout: int = 300) -> bytes:
    """用 curl 下载（Windows 上 Python 的 SSL 栈连部分镜像会 EOF，curl 正常）。"""
    import subprocess

    r = subprocess.run(
        ["curl", "-sL", "--max-time", str(timeout), "-A", UA["User-Agent"], url],
        capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"curl rc={r.returncode} {r.stderr[:200]!r}")
    if not r.stdout:
        raise RuntimeError("empty body")
    return r.stdout


def load_index(mirror: str) -> dict[str, dict]:
    """返回 {包名: {version, filename, size, depends:[...], provides:[...]}}"""
    import gzip

    base = f"{mirror}/dists/stable/main/binary-{ARCH}"
    text = None
    for suffix, dec in (("Packages.gz", gzip.decompress), ("Packages.xz", lzma.decompress), ("Packages", lambda b: b)):
        url = f"{base}/{suffix}"
        try:
            raw = fetch(url)
            text = dec(raw).decode("utf-8", "replace")
            print(f"[index] {url} ({len(raw) / 1024:.0f} KB)")
            break
        except Exception as e:  # noqa: BLE001
            print(f"[index] {suffix} 失败：{e}", file=sys.stderr)
    if text is None:
        raise RuntimeError(f"{mirror} 没有可用的 Packages 索引")
    pkgs: dict[str, dict] = {}
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        fields: dict[str, str] = {}
        key = None
        for line in block.splitlines():
            if line.startswith(" ") and key:
                continue  # 续行，忽略（Description 之类）
            if ":" in line:
                key, _, val = line.partition(":")
                fields[key.strip()] = val.strip()
        name = fields.get("Package")
        if not name:
            continue
        pkgs[name] = {
            "version": fields.get("Version", ""),
            "filename": fields.get("Filename", ""),
            "size": int(fields.get("Size", "0") or 0),
            "depends": parse_deps(fields.get("Depends", "")),
            "provides": parse_deps(fields.get("Provides", "")),
        }
    return pkgs


def parse_deps(s: str) -> list[str]:
    out = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        # "a | b" 取第一个；去掉版本约束
        cand = part.split("|")[0].strip()
        name = re.split(r"[\s(]", cand)[0].strip()
        if name:
            out.append(name)
    return out


def resolve(pkgs: dict[str, dict], roots: list[str]) -> list[str]:
    """广度优先闭包。找不到的包会报告。"""
    providers: dict[str, str] = {}
    for name, info in pkgs.items():
        for p in info["provides"]:
            providers.setdefault(p, name)

    need: list[str] = []
    seen: set[str] = set()
    missing: set[str] = set()
    queue = list(roots)
    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        if name not in pkgs:
            real = providers.get(name)
            if real and real not in seen:
                name = real
            else:
                missing.add(name)
                continue
        seen.add(name)
        need.append(name)
        for d in pkgs[name]["depends"]:
            if d not in seen:
                queue.append(d)
    if missing:
        print(f"[warn] 解析不到的依赖：{', '.join(sorted(missing))}", file=sys.stderr)
    return need


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("packages", nargs="+", help="要装的包名，如 python")
    ap.add_argument("--out", default="_tmp/termux-debs")
    ap.add_argument("--mirror", default=None)
    ap.add_argument("--list-only", action="store_true")
    args = ap.parse_args()

    mirrors = [args.mirror] if args.mirror else DEFAULT_MIRRORS
    pkgs = None
    used = None
    for m in mirrors:
        try:
            pkgs = load_index(m)
            used = m
            break
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {m} 不可用：{e}", file=sys.stderr)
    if not pkgs:
        print("所有镜像都不可用", file=sys.stderr)
        return 1
    print(f"[index] {len(pkgs)} 个包，来自 {used}")

    need = resolve(pkgs, args.packages)
    total = sum(pkgs[n]["size"] for n in need)
    print(f"[plan] {len(need)} 个包，共 {total / 1048576:.1f} MB")
    for n in need:
        print(f"  {n:32s} {pkgs[n]['version']:22s} {pkgs[n]['size'] / 1024:8.0f} KB")
    if args.list_only:
        return 0

    os.makedirs(args.out, exist_ok=True)
    ok = 0
    for n in need:
        info = pkgs[n]
        # Windows 不允许冒号（会被当作 NTFS ADS），Termux 里有 epoch 版本号形如 1:3.6.5
        fname = os.path.basename(info["filename"]).replace(":", "_")
        dst = os.path.join(args.out, fname)
        if os.path.exists(dst) and os.path.getsize(dst) == info["size"]:
            print(f"[skip] {fname}")
            ok += 1
            continue
        url = f"{used}/{info['filename']}"
        try:
            data = fetch(url)
        except Exception as e:  # noqa: BLE001
            print(f"[fail] {n}: {e}", file=sys.stderr)
            continue
        with open(dst, "wb") as f:
            f.write(data)
        print(f"[get ] {fname} ({len(data) / 1024:.0f} KB)")
        ok += 1

    print(f"\n[done] {ok}/{len(need)} 个包 → {os.path.abspath(args.out)}")
    print("下一步：")
    print(f"  adb push {args.out} /sdcard/Download/termux-debs")
    print("  # Termux 里：")
    print("  cd ~/storage/downloads/termux-debs && apt install ./*.deb")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

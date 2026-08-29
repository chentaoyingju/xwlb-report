#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
页面抓取助手（纯标准库）。用于补充 web_search 的文字稿采集：直接抓取
央视网 / 财联社 / 澎湃 / 腾讯 等页面，落地为 HTML 供解析核对条目清单。

用法:
    python scripts/fetch_pages.py --out <目录> --url <URL> [--url <URL> ...]

行为:
    逐条抓取，单个失败不影响其余；打印每个 URL 的结果（大小或错误）。
退出码: 0 = 至少成功 1 个; 2 = 全部失败
"""
from __future__ import annotations

import argparse
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def fetch(url: str, outfile: Path, timeout: int = 25) -> int:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        data = resp.read()
    outfile.write_bytes(data)
    return len(data)


def main() -> int:
    p = argparse.ArgumentParser(description="抓取页面为 HTML")
    p.add_argument("--out", required=True, help="输出目录")
    p.add_argument("--url", action="append", required=True, help="URL（可多次）")
    args = p.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    ok = 0
    for i, url in enumerate(args.url, start=1):
        fname = f"{i:02d}_{stamp}_{url.split('/')[-1][:60] or 'page'}.html"
        fname = "".join(c for c in fname if c.isalnum() or c in "._-")
        dest = out / fname
        try:
            size = fetch(url, dest)
            ok += 1
            print(f"[OK] {size} bytes <- {url}\n     saved: {dest}", flush=True)
        except urllib.error.HTTPError as exc:
            print(f"[ERR] HTTP {exc.code} <- {url}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[ERR] {exc} <- {url}", flush=True)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())

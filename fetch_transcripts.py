#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把已入库得到大脑的播客笔记，逐字稿拉取到本地 transcripts/raw/。

逐字稿在 `getnote note original <id>` 的 `data.original` 字段里，
形如「内容总时长:1小时9分钟」+ `[00:00 - 00:10] 文本` 的带时间戳正文。

用法：
  python3 fetch_transcripts.py              # 拉取全部
  python3 fetch_transcripts.py --days 7     # 只拉最近 N 天发布的
  python3 fetch_transcripts.py --force      # 已存在也重拉
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PUSHED = HERE / "pushed.json"
RAW = HERE / "transcripts" / "raw"
CST = timezone(timedelta(hours=8))


def safe(name: str, limit: int = 50) -> str:
    """文件名安全化。"""
    n = re.sub(r'[/\\:*?"<>|]', "_", name)
    n = re.sub(r"\s+", " ", n).strip()
    return n[:limit]


def getnote() -> str:
    b = shutil.which("getnote")
    if not b:
        print("找不到 getnote CLI", file=sys.stderr)
        sys.exit(2)
    return b


def fetch_original(binary: str, note_id: str, attempts: int = 6) -> str:
    """读逐字稿。

    注意：大笔记的 `note original` 经常在 API 层返回
    `context deadline exceeded (Client.Timeout exceeded while awaiting headers)`，
    实测重试到第 4 次就能拿到（2.5 小时播客约 6.5 万字）。
    **读操作重试是安全的**（幂等，不会产生重复笔记）——与写操作相反。
    """
    for i in range(attempts):
        try:
            p = subprocess.run([binary, "note", "original", note_id, "-o", "json"],
                               capture_output=True, text=True, timeout=180)
            d = json.loads(p.stdout or "{}")
            o = ((d.get("data") or {}).get("original") or "").strip()
            if o:
                return o
        except (subprocess.TimeoutExpired, ValueError):
            pass
        if i < attempts - 1:
            time.sleep(2 + i * 2)
    return ""


def unique_name(raw_dir: Path, base: str, used: set[str]) -> str:
    """分配不冲突的文件名（同档多期可能同日期，必须防撞名）。"""
    cand = f"{base}.md"
    n = 2
    while cand in used or (raw_dir / cand).exists():
        cand = f"{base}-{n}.md"
        n += 1
    return cand


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="只处理最近 N 天发布的，0=全部")
    ap.add_argument("--force", action="store_true", help="已存在也重拉")
    args = ap.parse_args()

    binary = getnote()
    data = json.loads(PUSHED.read_text(encoding="utf-8"))
    rows = [v for v in data.values() if v.get("status") == "ok" and v.get("note_id")]
    if args.days:
        cutoff = (datetime.now(CST) - timedelta(days=args.days)).date().isoformat()
        rows = [v for v in rows
                if (v.get("pub") or (v.get("pushed_at") or "")[:10] or "9999") >= cutoff]
    rows.sort(key=lambda v: ({"S": 0, "A": 1, "B": 2}.get(v.get("rating", ""), 3),
                             v.get("pub", "")))

    RAW.mkdir(parents=True, exist_ok=True)
    # 清单：eid → 文件名，保证多次运行文件名稳定、不互相覆盖
    mpath = HERE / "transcripts" / "manifest.json"
    manifest = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else {}
    used: set[str] = set(manifest.values())
    done, skipped, failed = [], [], []
    for v in rows:
        eid = next((k for k, x in data.items() if x is v), "")
        pod, title = v.get("podcast", ""), v.get("raw_title") or v.get("title", "")
        date = v.get("pub") or (v.get("pushed_at") or "")[:10]
        if eid in manifest and manifest[eid]:
            fname = manifest[eid]
        else:
            fname = unique_name(RAW, f"{date}-{safe(pod, 22)}", used)
            manifest[eid] = fname
            used.add(fname)
        out = RAW / fname
        if out.exists() and not args.force:
            n = len(out.read_text(encoding="utf-8"))
            print(f"  已存在  {fname}  ({n} 字)", file=sys.stderr)
            skipped.append((v, out, n))
            continue
        txt = fetch_original(binary, v["note_id"])
        if not txt:
            print(f"  无逐字稿  {pod}", file=sys.stderr)
            failed.append((v, "no original"))
            continue
        head = [
            "---",
            f"podcast: {pod}",
            f"title: {title}",
            f"date: {date}",
            f"rating: {v.get('rating', '')}",
            f"duration_sec: {v.get('duration') or 0}",
            f"note_url: {v.get('note_url', '')}",
            f"fetched_at: {datetime.now(CST):%Y-%m-%d %H:%M}",
            "---",
            "",
            f"# {title}",
            "",
            f"- 播客：{pod}",
            f"- 日期：{date}",
            f"- 优先级：{v.get('rating', '')}",
            f"- 得到大脑笔记：{v.get('note_url', '')}",
            "",
            "## 原始逐字稿（带时间戳）",
            "",
        ]
        out.write_text("\n".join(head) + txt + "\n", encoding="utf-8")
        print(f"  已拉取  {fname}  ({len(txt)} 字)", file=sys.stderr)
        done.append((v, out, len(txt)))

    # 索引
    idx = ["# 播客逐字稿索引", "",
           f"共 {len(rows)} 期 · 更新于 {datetime.now(CST):%Y-%m-%d %H:%M}", "",
           "| 日期 | 优先级 | 播客 | 单集 | 逐字稿 |", "|---|---|---|---|---|"]
    for v in rows:
        pod, title = v.get("podcast", ""), v.get("raw_title") or v.get("title", "")
        date = v.get("pub") or (v.get("pushed_at") or "")[:10]
        eid = next((k for k, x in data.items() if x is v), "")
        f = manifest.get(eid, "")
        ok = bool(f) and (RAW / f).exists()
        idx.append(f"| {date} | {v.get('rating','')} | {pod} | "
                   f"{title.replace('|','\\|')} | {'[' + f + '](raw/' + f + ')' if ok else '—'} |")
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    (HERE / "transcripts" / "INDEX.md").write_text("\n".join(idx) + "\n", encoding="utf-8")

    print(f"\n完成：新拉 {len(done)} · 跳过 {len(skipped)} · 失败 {len(failed)}", file=sys.stderr)
    print(f"目录：{RAW}")
    return 0


def raw_name(v: dict, date: str | None = None) -> str:
    date = date or v.get("pub") or (v.get("pushed_at") or "")[:10]
    return f"{date}-{safe(v.get('podcast',''), 22)}.md"


if __name__ == "__main__":
    sys.exit(main())

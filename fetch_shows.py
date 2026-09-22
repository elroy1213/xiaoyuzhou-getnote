#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取每档节目的实测数据，写入 show-cache.json（持久化在技能目录，不依赖 /tmp）。

记录：更新间隔（最近 15 期的中位间隔）、平均时长、最近更新日期、订阅数、总集数、
    账号原始简介（brief）、主播昵称与简介。
用法：
  python3 fetch_shows.py [--concurrency 8]
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from xyz_tracker import (  # noqa: E402
    NEXT_DATA_RE, fetch_all, http_get, load_profiles, load_watchlist, to_dt,
)

HERE = Path(__file__).resolve().parent
CACHE = HERE / "show-cache.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()

    sources = load_watchlist(HERE / "podcasts.txt")
    prof = load_profiles()
    print(f"抓取 {len(sources)} 档…", file=sys.stderr)
    results = fetch_all(sources, args.concurrency, prof)

    out = []
    # 上次成功的缓存：本次抓失败的档位保留旧数据，避免用错误结果覆盖好数据
    prev: dict[str, dict] = {}
    if CACHE.exists():
        try:
            prev = {x["key"]: x for x in json.loads(CACHE.read_text(encoding="utf-8"))
                    if "error" not in x}
        except (ValueError, KeyError):
            prev = {}

    for r in results:
        if not r["ok"]:
            old = prev.get(r["key"])
            if old:
                keep = dict(old)
                keep["stale"] = True
                out.append(keep)
                print(f"  保留旧缓存: {old['title']}（{r.get('error')}）", file=sys.stderr)
            else:
                out.append({"key": r["key"], "title": r.get("note") or r["key"],
                            "domain": r.get("domain", ""), "error": r.get("error")})
            continue
        eps = r["episodes"]
        dates = sorted((d for d in (to_dt(e["pub"]) for e in eps) if d), reverse=True)
        gaps = [(dates[i] - dates[i + 1]).days for i in range(len(dates) - 1)]
        gaps = [g for g in gaps if g >= 0]
        durs = [e["duration"] for e in eps if e["duration"]]
        p = r.get("profile") or {}
        out.append({
            "key": r["key"], "title": r["title"], "domain": r["domain"],
            "kind": p.get("kind", ""), "risk": p.get("risk", ""), "org": p.get("org", ""),
            "tip": p.get("tip", ""), "role": p.get("role", ""), "author": p.get("author", ""),
            "subs": int(p["subs"]) if p.get("subs") else None,
            "source": r["kind"], "priority": r.get("priority", False),
            "freq_days": round(statistics.median(gaps), 1) if gaps else None,
            "avg_min": round(statistics.mean(durs) / 60) if durs else None,
            "n_recent": len(eps),
            "last_pub": dates[0].strftime("%Y-%m-%d") if dates else "",
            "episode_count": r.get("episode_count"),
            "brief": re.sub(r"\s+", " ", r.get("brief") or ""),
            "podcasters": r.get("podcasters") or [],
        })

    CACHE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(1 for x in out if "error" not in x)
    print(f"完成 {ok}/{len(out)} → {CACHE}", file=sys.stderr)
    for x in out:
        if "error" in x:
            print("  失败:", x["title"], x.get("error"), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回填 pushed.json 缺失的字段（pub 发布日期 / duration 时长 / domain 领域）。

为什么需要：早期版本的 push_to_getnote.py 未记录这些字段，导致
1) 无法按发布日期给逐字稿文件命名（同档多期会撞名互相覆盖）
2) 情报清单里日期、时长显示为空

做法：重新扫一遍所有来源，建立 eid → (pub, duration) 映射后回填。

用法：python3 backfill_pushed.py [--concurrency 8]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from xyz_tracker import fetch_all, load_profiles, load_ratings, load_watchlist, to_dt  # noqa: E402

PUSHED = HERE / "pushed.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()

    if not PUSHED.exists():
        print("没有 pushed.json", file=sys.stderr)
        return 1
    data = json.loads(PUSHED.read_text(encoding="utf-8"))
    need = [k for k, v in data.items()
            if not v.get("pub") or not v.get("duration") or not v.get("domain")]
    if not need:
        print("无需回填，字段齐全。")
        return 0
    print(f"需要回填 {len(need)} 条，扫描来源…", file=sys.stderr)

    sources = load_watchlist(HERE / "podcasts.txt")
    dom_of = {s["key"]: s["domain"] for s in sources}
    results = fetch_all(sources, args.concurrency, load_profiles(), load_ratings())

    # eid → (pub, duration, domain, podcast_title)
    idx: dict[str, tuple] = {}
    for r in results:
        if not r["ok"]:
            continue
        for ep in r["episodes"]:
            dt = to_dt(ep["pub"])
            idx[ep["id"]] = (
                dt.strftime("%Y-%m-%d") if dt else "",
                ep["duration"] or 0,
                dom_of.get(r["key"], ""),
                r["title"],
            )

    filled = 0
    for k in need:
        v = data[k]
        hit = idx.get(k)
        if not hit:
            print(f"  未找到单集（可能已滚出最近 15 期）：{v.get('podcast','')} "
                  f"{(v.get('raw_title') or v.get('title',''))[:30]}", file=sys.stderr)
            continue
        pub, dur, dom, pod = hit
        if not v.get("pub") and pub:
            v["pub"] = pub
        if not v.get("duration") and dur:
            v["duration"] = dur
        if not v.get("domain") and dom:
            v["domain"] = dom
        if not v.get("podcast") and pod:
            v["podcast"] = pod
        filled += 1
        print(f"  回填 {v.get('podcast','')[:20]:<22} pub={pub} dur={dur}s domain={dom}",
              file=sys.stderr)

    PUSHED.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n已回填 {filled}/{len(need)} 条 → {PUSHED}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

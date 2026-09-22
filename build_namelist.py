#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成《评级名单》：按 S / A / B / 未评 分层，附每周产出量估算。

数据来源：ratings.csv（评级与领域）+ show-cache.json（节奏、订阅、最近更新）
用法：python3 build_namelist.py
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from xyz_tracker import load_watchlist  # noqa: E402

TODAY = date(2026, 9, 22)
DOMAINS = ["专业领域", "职场管理", "个人生活"]
BANDS = [("S", "S 级 —— 每期必听 / 必读摘要"),
         ("A", "A 级 —— 挑着听"),
         ("B", "B 级 —— 偶尔扫标题"),
         ("", "未评级 —— 待定")]


def per_week(freq: float | None) -> float:
    """按更新间隔估算每周产出期数。"""
    if not freq or freq <= 0:
        return 0.0
    return min(7.0 / freq, 7.0)


def main() -> int:
    shows = {s["key"]: s for s in json.loads((HERE / "show-cache.json").read_text(encoding="utf-8"))
             if "error" not in s}
    dom_of = {s["key"]: s["domain"] for s in load_watchlist(HERE / "podcasts.txt")}
    rows = []
    with (HERE / "ratings.csv").open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            s = shows.get(r["key"], {})
            days = None
            if s.get("last_pub"):
                try:
                    days = (TODAY - date.fromisoformat(s["last_pub"])).days
                except ValueError:
                    days = None
            rows.append({
                "title": r["title"], "key": r["key"],
                "domain": dom_of.get(r["key"]) or r["domain"],
                "kind": r.get("kind", ""), "risk": r.get("risk", ""), "org": s.get("org", ""),
                "rating": (r.get("评级") or "").strip().upper(),
                "freq": s.get("freq_days"), "avg_min": s.get("avg_min"),
                "days": days, "last": s.get("last_pub", ""),
                "note": (r.get("备注") or "").strip(),
            })

    def dot(d):
        if d is None:
            return "⚪"
        return "🟢" if d <= 30 else ("🟡" if d <= 180 else "🔴")

    L = ["# 评级名单", "",
         f"共 {len(rows)} 档。按你的 S / A / B 评级分层，附实测更新节奏。", "",
         "**标记**：🟢 30 天内更新 ／ 🟡 31–180 天 ／ 🔴 超 180 天未更新", ""]

    # 总览
    total_wk = 0.0
    L += ["## 总览", "", "| 评级 | 档数 | 每周产出（估算） | 说明 |", "|---|---|---|---|"]
    for band, label in BANDS:
        g = [r for r in rows if r["rating"] == band]
        wk = sum(per_week(r["freq"]) for r in g)
        total_wk += wk
        L.append(f"| **{band or '未评'}** | {len(g)} | 约 {wk:.1f} 期/周 | {label} |")
    L += ["", f"合计约 **{total_wk:.0f} 期/周**。", ""]

    for band, label in BANDS:
        g = [r for r in rows if r["rating"] == band]
        if not g:
            continue
        wk = sum(per_week(r["freq"]) for r in g)
        L += [f"## {label}（{len(g)} 档 · 约 {wk:.1f} 期/周）", ""]
        for dom in DOMAINS:
            dg = [r for r in g if r["domain"] == dom]
            if not dg:
                continue
            dg.sort(key=lambda r: (-(r["freq"] and 1 / r["freq"] or 0), r["title"]))
            L += [f"### {dom}（{len(dg)} 档）", "",
                  "| 播客 | 出品方 | 性质 · 立场 | 节奏 | 时长 | 最新 |", "|---|---|---|---|---|---|"]
            for r in dg:
                risk = f"{r['kind']}" + (f" · 立场{r['risk']}" if r["risk"] and r["risk"] != "低" else "")
                freq = f"{r['freq']} 天/期" if r["freq"] else "—"
                dur = f"{r['avg_min']} 分" if r["avg_min"] else "—"
                last = f"{dot(r['days'])} {r['last'] or '—'}"
                L.append(f"| {r['title']} | {r['org'] or '—'} | {risk} | {freq} | {dur} | {last} |")
            L.append("")

    # 提醒
    s_rows = [r for r in rows if r["rating"] == "S"]
    s_inst = [r for r in s_rows if r["kind"] == "机构自营"]
    dormant = [r for r in rows if (r["days"] or 0) > 180]
    L += ["## 三点提醒", ""]
    if s_inst:
        L.append(f"**1. S 级里有 {len(s_inst)} 档是机构自营号**——你选它说明内容够好，"
                 "但读的时候要记得那是自家视角："
                 + "、".join(f"{r['title']}（{r['org']}·立场{r['risk']}）" for r in s_inst))
    else:
        L.append("**1. S 级里没有机构自营号，全是你判断过的独立信源或一线从业者。**")
    L.append("")
    if dormant:
        L.append(f"**2. {len(dormant)} 档已停更超 180 天。**这些不用打分，直接决定去留即可："
                 + "、".join(f"{r['title']}（{(r['days'] or 0)} 天）" for r in
                            sorted(dormant, key=lambda x: -(x["days"] or 0))))
    L.append("")
    s_wk = sum(per_week(r["freq"]) for r in s_rows)
    s_min = sum(per_week(r["freq"]) * (r["avg_min"] or 0) for r in s_rows)
    L.append(f"**3. S 级的时间成本：{s_wk:.1f} 期/周、{s_min / 60:.1f} 小时/周**，"
             f"约合每天 {s_min / 60 / 7:.1f} 小时。这是全力跟的上限——"
             "超出部分必须靠摘要，否则会挤掉输出时间。")
    L += ["", "---", "",
          "_名单由 `ratings.csv` 生成；改评级后重跑 `python3 build_namelist.py` 即可更新。_"]

    (HERE / "评级名单.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"生成完成：评级名单.md（{len(rows)} 档）")
    print(f"  S {len(s_rows)} / A {sum(1 for r in rows if r['rating'] == 'A')} "
          f"/ B {sum(1 for r in rows if r['rating'] == 'B')} "
          f"/ 未评 {sum(1 for r in rows if not r['rating'])}")
    print(f"  S 级时间成本：约 {s_min / 60:.1f} 小时/周")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

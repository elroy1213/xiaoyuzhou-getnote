#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成《播客清单与评级》文档 + ratings.csv（供用户打 S/A/B）。

数据来源：
  - /tmp/showdata.json  实测节奏数据（更新间隔、平均时长、最近更新、订阅数）
  - /tmp/profiles.json  小宇宙一手主播资料（author / podcasters / brief）
用法：
  python3 build_roster.py            # 用缓存数据生成
"""
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from xyz_tracker import load_watchlist  # noqa: E402

TODAY = date(2026, 9, 22)

DOMAIN_ORDER = ["专业领域", "职场管理", "个人生活"]
KIND_ORDER = ["机构自营", "独立媒体", "个人IP", "品牌播客", "二手工具号"]


def activity(days: int | None) -> tuple[str, str]:
    """返回 (标记, 说明)。"""
    if days is None:
        return "⚪", "无数据"
    if days <= 30:
        return "🟢", f"活跃 · {days} 天前更新"
    if days <= 180:
        return "🟡", f"间歇 · {days} 天前更新"
    return "🔴", f"疑似停更 · {days} 天前更新"


def clean_brief(text: str, limit: int = 150) -> str:
    if not text:
        return ""
    t = re.sub(r"https?://\S+", "", text)
    t = re.sub(r"[【\[]?[^】\]]{0,12}(听友群|微信号|公众号|加群|联系|商务|合作)[^。！？]{0,40}[】\]]?", "", t)
    t = re.sub(r"\s+", " ", t).strip(" 。;；,")
    if not t:
        return ""
    # 取前两个句子
    parts = re.split(r"(?<=[。！？!?])", t)
    out = ""
    for p in parts:
        if len(out) + len(p) > limit:
            break
        out += p
    return (out or t[:limit]).strip()


def main() -> int:
    shows = json.loads((HERE / "show-cache.json").read_text(encoding="utf-8"))
    # profiles.csv 提供权威的出品方 / 性质 / 立场 / 读法建议
    with (HERE / "profiles.csv").open(encoding="utf-8", newline="") as f:
        profs = {r["key"]: r for r in csv.DictReader(f)}

    rows = []
    # 领域以 podcasts.txt 为唯一准绳（缓存里的 domain 可能已过期）
    dom_of = {s["key"]: s["domain"] for s in load_watchlist(HERE / "podcasts.txt")}
    for s in shows:
        if "error" in s:
            continue
        p = profs.get(s["key"], {})
        merged = {**s}
        merged["domain"] = dom_of.get(s["key"], s.get("domain", ""))
        for k in ("org", "kind", "risk", "tip"):
            merged[k] = p.get(k) or s.get(k, "")
        days = None
        if s.get("last_pub"):
            try:
                days = (TODAY - date.fromisoformat(s["last_pub"])).days
            except ValueError:
                days = None
        merged["days"] = days
        merged["brief"] = clean_brief(s.get("brief", ""))
        rows.append(merged)

    # ── ratings.csv（保留用户已填的评级与备注，避免重跑被覆盖）──
    kept: dict[str, tuple[str, str]] = {}
    rpath = HERE / "ratings.csv"
    if rpath.exists():
        try:
            with rpath.open(encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("key"):
                        kept[row["key"]] = (row.get("评级", ""), row.get("备注", ""))
        except (ValueError, KeyError):
            kept = {}
    saved = sum(1 for v in kept.values() if v[0].strip())

    with rpath.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["key", "title", "domain", "kind", "risk", "评级", "备注"])
        for r in rows:
            old = kept.get(r["key"], ("", ""))
            w.writerow([r["key"], r["title"], r["domain"], r["kind"], r["risk"], old[0], old[1]])
    if saved:
        print(f"已保留 {saved} 条已有评级")

    # ── 文档 ────────────────────────────────────────
    L = ["# 播客清单与评级", "",
         f"共 **{len(rows)} 档**。每条给你四样东西：**谁做的 / 多大 / 什么节奏 / 怎么读**，",
         "末尾留 S / A / B 三档让你打。", "",
         "**活跃度标记**：🟢 30 天内更新 ／ 🟡 31–180 天 ／ 🔴 超过 180 天未更新（疑似停更）", "",
         "**评级建议口径**（你可以另定）：",
         "- **S** — 每期都听或必读摘要，是你认知/判断的一手来源",
         "- **A** — 挑着听，选题对胃口时听",
         "- **B** — 偶尔扫一眼标题，或直接不看", ""]

    warn = [r for r in rows if (r["days"] or 0) > 180]
    if warn:
        L += [f"> ⚠️ **先提醒：{len(warn)} 档已超过 180 天未更新**，其中包括几档你标过 ★ 的。",
              "> 给这些打分意义不大，建议直接决定「留作历史查阅」还是「从清单移除」。",
              "> 停更最久的：" + "、".join(f"{r['title']}（{r['days']} 天）"
                                          for r in sorted(warn, key=lambda x: -(x["days"] or 0))[:6]),
              ""]

    for dom in DOMAIN_ORDER:
        g = [r for r in rows if r["domain"] == dom]
        if not g:
            continue
        g.sort(key=lambda r: (-(3 if (r["days"] is not None and r["days"] <= 30)
                                else 2 if (r["days"] is not None and r["days"] <= 180) else 1),
                              -(r["subs"] or 0)))
        act = sum(1 for r in g if (r["days"] or 999) <= 30)
        L += [f"# {dom}", "",
              f"{len(g)} 档，其中 {act} 档活跃（30 天内更新过）。", ""]
        for i, r in enumerate(g, 1):
            mark, act_txt = activity(r["days"])
            subs = f"{r['subs'] / 10000:.1f} 万订阅" if r.get("subs") else "订阅数未公开"
            total = f"{r['episode_count']} 期" if r.get("episode_count") else ""
            freq = f"约 {r['freq_days']} 天一期" if r.get("freq_days") else "节奏未知"
            dur = f"平均 {r['avg_min']} 分钟" if r.get("avg_min") else ""
            last = r.get("last_pub") or "无"
            risk = r.get("risk") or ""
            risk_txt = f" · 立场 {risk}" if risk and risk != "低" else " · 立场低"
            L += [f"## {i}. {r['title']}", "",
                  f"- **谁做的**：{r.get('org') or '—'}　｜　{r.get('kind') or '—'}{risk_txt}",
                  f"- **规模**：{subs}" + (f" · {total}" if total else ""),
                  f"- **节奏**：{freq}" + (f" · {dur}" if dur else "") + f"　｜　**最新一期**：{last}",
                  f"- **状态**：{mark} {act_txt}"]
            if r["brief"]:
                L.append(f"- **它是什么**：{r['brief']}")
            if r.get("tip"):
                L.append(f"- **怎么读**：{r['tip']}")
            L += ["", "`评级：☐ S　☐ A　☐ B`", ""]

    (HERE / "播客清单与评级.md").write_text("\n".join(L) + "\n", encoding="utf-8")

    from collections import Counter
    print(f"生成完成：{len(rows)} 档")
    print("活跃度:", dict(Counter(activity(r['days'])[1].split(' ·')[0] for r in rows)))
    print(f"已写：播客清单与评级.md / ratings.csv（评级列留空待填）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把小宇宙新单集链接推送到「得到大脑」（Get笔记），按工作目的分类归档。

为什么走得到大脑而不是自建 ASR
------------------------------
实测（2026-09）得到大脑能直接处理小宇宙单集链接，自动完成：
  - 抓取音频并**转写**（69 分钟一期 → 约 3.6 万字带时间戳逐字稿）
  - 生成**结构化 AI 摘要**（按主题分组的要点，约 8 千字）
无需本地模型、无需下载音频、无需任何 ASR 配置。

两个必须知道的坑
----------------
1. **CLI 约 30 秒超时报错，但服务端其实已经成功。**
   会返回 `context deadline exceeded` / `retryable: false`，但稍后 `getnote notes`
   就能查到。→ 超时后**禁止重试**，必须用 `--verify` 回查，否则会产生重复笔记。
2. **`note transcript` 对链接类笔记不可用**，逐字稿要从 `note original` 取。

用法
----
  # 先看会推什么（不写入）
  python3 push_to_getnote.py --since 7d --dry-run

  # 正式推送 S/A 级新单集
  python3 push_to_getnote.py --since 7d

  # 只推 S 级，并指定知识库
  python3 push_to_getnote.py --since 7d --ratings S --kb JbBKVyOJ

  # 核实上一轮可能超时的推送（超时后必须跑这个，不要重试）
  python3 push_to_getnote.py --verify
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from xyz_tracker import (  # noqa: E402
    DOMAINS, UNCLASSIFIED, fetch_all, load_profiles, load_ratings,
    load_watchlist, to_dt,
)

CST = timezone(timedelta(hours=8))
PUSHED = HERE / "pushed.json"
CONFIG = HERE / "getnote.json"
REPORT_DIR = HERE / "reports"

DEFAULT_CONFIG = {
    "default_kb": "",
    "domain_kb": {},
    "ratings": ["S", "A"],
    "title_prefix": True,
    "exclude_kinds": ["二手工具号"],
}


def log(msg: str = "") -> None:
    print(msg, file=sys.stderr)


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG.exists():
        try:
            cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
        except ValueError:
            log(f"getnote.json 解析失败，用默认配置")
    return cfg


def getnote_bin() -> str:
    b = shutil.which("getnote")
    if not b:
        log("找不到 getnote CLI。请先安装并授权：npm i -g @getnote/cli && getnote setup")
        sys.exit(2)
    return b


# --------------------------------------------------------------------------
# 推送
# --------------------------------------------------------------------------
def make_title(rating: str, podcast: str, title: str, prefix: bool = True) -> str:
    """标题前标注优先级 S/A/B —— 用户要求，方便在得到大脑里扫。"""
    tag = f"[{rating}] " if (prefix and rating) else ""
    return f"{tag}{podcast}｜{title}"[:120]


def save_one(binary: str, url: str, title: str, kb: str, tag: str) -> dict:
    """调用 getnote save。返回 {ok, note_id, status, error}。

    status: ok | pending（客户端超时但服务端可能已完成，需核实） | failed
    """
    cmd = [binary, "save", url, "--title", title, "-o", "json"]
    if kb:
        cmd += ["--topic-id", kb]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return {"tag": tag, "status": "pending", "error": "subprocess timeout"}

    out = (p.stdout or "").strip()
    try:
        d = json.loads(out)
    except ValueError:
        msg = (out or p.stderr or "")[:200]
        return {"tag": tag, "status": "pending" if "deadline" in msg.lower() else "failed",
                "error": msg}

    if d.get("success") and (d.get("data") or {}).get("note", {}).get("note_id"):
        n = d["data"]["note"]
        return {"tag": tag, "status": "ok", "note_id": str(n["note_id"]),
                "note_url": n.get("note_url", ""), "title": n.get("title", title)}

    err = json.dumps(d.get("error") or {}, ensure_ascii=False)
    if p.returncode != 0 or "deadline" in err.lower() or "timeout" in err.lower():
        # 客户端超时 ≠ 服务端失败，需要用 --verify 回查
        return {"tag": tag, "status": "pending", "error": err[:200]}
    return {"tag": tag, "status": "failed", "error": err[:200]}


def list_recent_titles(binary: str, max_pages: int = 12, per_page: int = 20) -> dict[str, dict]:
    """读最近笔记，标题 → 笔记信息。用于核实 pending 的推送。

    注意：`getnote notes` **每页最多返回 20 条**（即使 --limit 传更大值），
    必须按返回的 cursor 翻页，否则回查会漏掉较早创建的笔记。
    """
    out: dict[str, dict] = {}
    cursor = ""
    for _ in range(max_pages):
        cmd = [binary, "notes", "--limit", str(per_page), "-o", "json"]
        if cursor:
            cmd += ["--cursor", cursor]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            d = json.loads(p.stdout or "{}")
        except (subprocess.TimeoutExpired, ValueError):
            break
        dd = d.get("data") or {}
        notes = dd.get("notes") or []
        if not notes:
            break
        for n in notes:
            t = (n.get("title") or "").strip()
            if t:
                out[t] = n
        if not dd.get("has_more"):
            break
        cursor = str(dd.get("cursor") or "")
        if not cursor:
            break
    return out


def match_title(want: str, have: dict[str, dict]) -> dict | None:
    """精确优先，退化为「前 30 字 + 结尾 10 字」双重匹配（服务端可能截断标题）。"""
    want = (want or "").strip()
    if not want:
        return None
    if want in have:
        return have[want]
    head, tail = want[:30], want[-10:]
    for k, v in have.items():
        if k.startswith(head) or (len(k) > 30 and tail and k.endswith(tail)):
            return v
    return None


def fetch_note_content(binary: str, note_id: str, limit: int = 700) -> str:
    """读回 AI 摘要（content）。"""
    try:
        p = subprocess.run([binary, "note", note_id, "-o", "json"],
                           capture_output=True, text=True, timeout=120)
        d = json.loads(p.stdout or "{}")
    except (subprocess.TimeoutExpired, ValueError):
        return ""
    c = ((d.get("data") or {}).get("note") or {}).get("content") or ""
    c = re.sub(r"^#+\s*", "", c, flags=re.M)
    c = re.sub(r"\n{2,}", "\n", c).strip()
    return c[:limit]


# --------------------------------------------------------------------------
def collect_candidates(args, cfg) -> list[dict]:
    sources = load_watchlist(HERE / "podcasts.txt")
    if args.domain:
        sources = [s for s in sources if s["domain"] == args.domain]
    if not sources:
        log("清单为空。先跑 build_roster.py 生成 podcasts.txt 并填好评级。")
        sys.exit(1)

    since = datetime.now(CST) - timedelta(days=args.days)
    log(f"扫描 {len(sources)} 档（最近 {args.days} 天）…")
    results = fetch_all(sources, args.concurrency, load_profiles(), load_ratings())

    pushed = json.loads(PUSHED.read_text(encoding="utf-8")) if PUSHED.exists() else {}
    want = {r.strip().upper() for r in args.ratings.split(",") if r.strip()}
    skip_kinds = set(cfg.get("exclude_kinds") or [])

    cands = []
    for r in results:
        if not r["ok"]:
            continue
        rating = (r.get("rating") or "").strip().upper()
        if want and rating not in want:
            continue
        kind = (r.get("profile") or {}).get("kind") or ""
        if kind in skip_kinds and not args.include_secondary:
            continue
        for ep in r["episodes"]:
            if ep["id"] in pushed:
                continue
            dt = to_dt(ep["pub"])
            if dt and dt < since:
                continue
            cands.append({
                "eid": ep["id"], "url": ep["url"], "title": ep["title"],
                "podcast": r["title"], "domain": r["domain"], "rating": rating,
                "kind": kind, "duration": ep["duration"], "pub_dt": dt,
                "note": r.get("note", ""),
            })
    cands.sort(key=lambda x: ({"S": 0, "A": 1, "B": 2}.get(x["rating"], 3),
                              x["pub_dt"] or datetime.min.replace(tzinfo=CST)))
    return cands


def pick_kb(c: dict, cfg: dict, cli_kb: str) -> str:
    if cli_kb:
        return cli_kb
    return (cfg.get("domain_kb") or {}).get(c["domain"]) or cfg.get("default_kb") or ""


def render_digest(sent: list[dict], pend: list[dict], failed: list[dict],
                  window: str) -> str:
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    L = [f"# 播客情报 · 已灌入得到大脑 · {now}", "",
         f"- 时间窗：{window}", f"- 已入库：**{len(sent)}** 期"
         + (f" · 待核实 {len(pend)} 期" if pend else "")
         + (f" · 失败 {len(failed)} 期" if failed else ""), ""]
    for dom in DOMAINS + [UNCLASSIFIED]:
        g = [x for x in sent if x["domain"] == dom]
        if not g:
            continue
        L += [f"## {dom}", ""]
        for x in g:
            tag = f"[{x['rating']}] " if x["rating"] else ""
            link = f" · [打开笔记]({x['note_url']})" if x.get("note_url") else ""
            L.append(f"### {tag}{x['podcast']} ｜ {x['title']}")
            L.append("")
            meta = f"{x['pub_dt'].strftime('%m-%d') if x['pub_dt'] else '-'}"
            if x.get("duration"):
                m = int(x["duration"]) // 60
                meta += f" · {m // 60}h{m % 60:02d}m" if m >= 60 else f" · {m}m"
            L.append(f"- {meta}{link}")
            if x.get("summary"):
                L.append(f"- **讲了什么**：{x['summary']}")
            L.append("")
    if pend:
        L += ["## 待核实（客户端超时，服务端可能已完成）", "",
              "跑 `python3 push_to_getnote.py --verify` 回查，**不要重新推送**。", ""]
        for x in pend:
            L.append(f"- [{x['rating']}] {x['podcast']} ｜ {x['title']}")
        L.append("")
    if failed:
        L += ["## 推送失败", ""]
        for x in failed:
            L.append(f"- {x['podcast']} ｜ {x['title']} —— {x.get('error', '')}")
        L.append("")
    return "\n".join(L)


def name_domain_map() -> dict[str, str]:
    """播客名 → 领域。用于兼容早期未记录 domain 字段的 pushed.json 条目。"""
    prof = load_profiles()
    m: dict[str, str] = {}
    for s in load_watchlist(HERE / "podcasts.txt"):
        title = (prof.get(s["key"]) or {}).get("title") or s.get("note") or ""
        if title:
            m[title] = s["domain"]
    return m


def raw_of(rec: dict) -> str:
    """取不带优先级前缀、不带播客名的单集原标题。"""
    t = rec.get("raw_title") or rec.get("title") or ""
    t = re.sub(r"^\[[SAB]\]\s*", "", t)
    pod = rec.get("podcast") or ""
    if pod and t.startswith(pod + "｜"):
        t = t[len(pod) + 1:]
    return t.strip()


def render_from_pushed(binary: str, days: int, with_summary: bool = False) -> Path:
    """从 pushed.json 重建情报清单（--verify 后刷新，补上回查确认的链接）。"""
    data = json.loads(PUSHED.read_text(encoding="utf-8")) if PUSHED.exists() else {}
    cutoff = (datetime.now(CST) - timedelta(days=days)).date().isoformat()
    dom_of = name_domain_map()
    ok = []
    for v in data.values():
        if v.get("status") != "ok":
            continue
        v = dict(v)
        if not v.get("domain"):
            v["domain"] = dom_of.get(v.get("podcast", ""), UNCLASSIFIED)
        stamp = v.get("pub") or (v.get("pushed_at") or "")[:10]
        if (stamp or "9999") < cutoff:
            continue
        ok.append(v)
    ok.sort(key=lambda v: ({"S": 0, "A": 1, "B": 2}.get(v.get("rating", ""), 3),
                           v.get("pub") or v.get("pushed_at", "")))
    lines = [f"# 播客情报 · 已灌入得到大脑 · {datetime.now(CST):%Y-%m-%d %H:%M}", "",
             f"- 时间窗：最近 {days} 天　|　已入库 **{len(ok)}** 期", "",
             "> 标题前的 `[S]`/`[A]` 是优先级。点「打开笔记」进得到大脑，可直接提问。", ""]
    for dom in DOMAINS + [UNCLASSIFIED]:
        g = [x for x in ok if (x.get("domain") or UNCLASSIFIED) == dom]
        if not g:
            continue
        lines += [f"## {dom}（{len(g)} 期）", ""]
        for x in g:
            tag = f"[{x['rating']}] " if x.get("rating") else ""
            lines.append(f"- **{tag}{x.get('podcast','')}｜{raw_of(x)}**")
            bits = []
            if x.get("pub"):
                bits.append(x["pub"])
            d = x.get("duration") or 0
            if d:
                m = int(d) // 60
                bits.append(f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m")
            if x.get("note_url"):
                bits.append(f"[打开笔记]({x['note_url']})")
            if bits:
                lines.append("  - " + " · ".join(bits))
            if with_summary and x.get("note_id"):
                s = fetch_note_content(binary, x["note_id"], 400)
                if s:
                    lines.append(f"  - **讲了什么**：{s}")
        lines.append("")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"podcast-digest-{datetime.now(CST):%Y-%m-%d}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def cmd_verify(binary: str, forget: bool = False, days: int = 7) -> int:
    """回查 pushed.json 里 pending 的条目。

    --forget：把仍未确认的 pending 记录删掉，让它们下次能被重新推送。
    只有在确认这些笔记**确实不存在**时才用；否则会产生重复。
    """
    if not PUSHED.exists():
        log("没有 pushed.json，无需核实。")
        return 0
    data = json.loads(PUSHED.read_text(encoding="utf-8"))
    pend = {k: v for k, v in data.items() if v.get("status") == "pending"}
    if not pend:
        log(f"没有待核实条目（共 {len(data)} 条记录）。")
        out = render_from_pushed(binary, days)
        log(f"情报清单已刷新：{out}")
        return 0
    log(f"待核实 {len(pend)} 条，翻页读取最近笔记比对…")
    recent = list_recent_titles(binary)
    log(f"  拉到 {len(recent)} 条笔记标题")
    fixed = 0
    for eid, v in pend.items():
        hit = match_title(v.get("title", ""), recent)
        if hit:
            v["status"] = "ok"
            v["note_id"] = str(hit.get("note_id", ""))
            v["note_url"] = hit.get("note_url", "")
            fixed += 1
            log(f"  已确认入库：{v.get('title','')[:50]}")
    still = [k for k, v in data.items() if v.get("status") == "pending"]
    for k in still:
        if forget:
            log(f"  移除未确认记录（将可重新推送）：{data[k].get('title','')[:50]}")
            del data[k]
    PUSHED.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"核实完成：新确认 {fixed} 条；仍未确认 {len(still)} 条"
        + ("（已按 --forget 移除）" if forget and still else
           "，稍后再跑一次 --verify" if still else ""))
    if still and not forget:
        log("若确认这些笔记确实不存在，加 --forget 移除记录以便重新推送。")
    out = render_from_pushed(binary, days)
    log(f"情报清单已刷新：{out}")
    return 0


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="把小宇宙新单集推送进得到大脑")
    ap.add_argument("--days", type=int, default=7, help="扫描最近 N 天，默认 7")
    ap.add_argument("--since", help="同 --days，也接受 7d / 24h 写法")
    ap.add_argument("--ratings", default="S,A", help="推送哪些评级，默认 S,A")
    ap.add_argument("--kb", default="", help="目标知识库 id（覆盖配置）")
    ap.add_argument("--domain", default="", help="只处理某个领域")
    ap.add_argument("--concurrency", type=int, default=3, help="并发提交数，默认 3")
    ap.add_argument("--include-secondary", action="store_true",
                    help="连二手/搬运号一起推（默认排除）")
    ap.add_argument("--dry-run", action="store_true", help="只列出，不写入")
    ap.add_argument("--verify", action="store_true", help="回查上一轮超时的推送")
    ap.add_argument("--forget", action="store_true",
                    help="配合 --verify：移除仍未确认的记录，使其可重新推送（确认笔记不存在时才用）")
    ap.add_argument("--no-summary", action="store_true", help="不回读摘要（更快）")
    args = ap.parse_args()
    if args.since:
        m = re.fullmatch(r"(\d+)\s*([dh])?", str(args.since).strip().lower())
        if m:
            n, unit = int(m.group(1)), (m.group(2) or "d")
            args.days = max(1, round(n / 24) if unit == "h" else n)

    binary = getnote_bin()
    if args.verify:
        return cmd_verify(binary, args.forget, args.days)

    cfg = load_config()
    cands = collect_candidates(args, cfg)

    if not cands:
        log("没有需要推送的新单集。")
        return 0

    log(f"待推送 {len(cands)} 期：")
    for c in cands:
        log(f"  [{c['rating'] or '-'}] {c['podcast'][:20]:<22} {c['title'][:44]}")

    if args.dry_run:
        log("\n--dry-run：未写入。去掉该参数即正式推送。")
        return 0

    pushed = json.loads(PUSHED.read_text(encoding="utf-8")) if PUSHED.exists() else {}

    def job(c: dict) -> tuple[dict, dict]:
        title = make_title(c["rating"], c["podcast"], c["title"], cfg.get("title_prefix", True))
        res = save_one(binary, c["url"], title, pick_kb(c, cfg, args.kb), c["eid"])
        res["title"] = title
        return c, res

    log(f"\n开始推送（并发 {args.concurrency}，每条约 30 秒）…")
    sent, pend, failed = [], [], []
    with futures.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        for i, (c, res) in enumerate(ex.map(job, cands), 1):
            rec = {"status": res["status"], "title": res["title"],
                   "note_id": res.get("note_id", ""), "note_url": res.get("note_url", ""),
                   "podcast": c["podcast"], "rating": c["rating"],
                   "domain": c["domain"], "kind": c["kind"],
                   "duration": c["duration"],
                   "pub": c["pub_dt"].strftime("%Y-%m-%d") if c["pub_dt"] else "",
                   "pushed_at": datetime.now(CST).isoformat(timespec="seconds")}
            if res.get("error"):
                rec["error"] = res["error"]
            pushed[c["eid"]] = rec
            flag = {"ok": "OK ", "pending": "PEND", "failed": "FAIL"}[res["status"]]
            log(f"  [{i}/{len(cands)}] {flag} {c['podcast'][:18]} {c['title'][:36]}")
            if res["status"] == "ok":
                sent.append({**c, "note_id": res["note_id"], "note_url": res.get("note_url", "")})
            elif res["status"] == "pending":
                pend.append({**c, "error": res.get("error", "")})
            else:
                failed.append({**c, "error": res.get("error", "")})

    PUSHED.write_text(json.dumps(pushed, ensure_ascii=False, indent=1), encoding="utf-8")

    # 回读摘要，让报告里有「大概讲了什么」
    if sent and not args.no_summary:
        log(f"\n回读 {len(sent)} 条的 AI 摘要…")
        for x in sent:
            if x.get("note_id"):
                x["summary"] = fetch_note_content(binary, x["note_id"])
                time.sleep(0.3)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"podcast-digest-{datetime.now(CST):%Y-%m-%d}.md"
    out.write_text(render_digest(sent, pend, failed, f"最近 {args.days} 天"), encoding="utf-8")

    print(f"已入库 {len(sent)} 期"
          + (f"，待核实 {len(pend)} 期" if pend else "")
          + (f"，失败 {len(failed)} 期" if failed else ""))
    if pend:
        print("⚠️ 有超时条目：跑 python3 push_to_getnote.py --verify 回查，不要重新推送。")
    print(f"报告已写入：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

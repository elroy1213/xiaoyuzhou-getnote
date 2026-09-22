#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小宇宙播客更新追踪器 · 按生活领域分组版

回答一个问题：我关心的播客里，哪些更新了？
不下载音频、不做转写、零第三方依赖。

三个领域（可自行增删）
--------------------
  [专业领域]  AI / 创投 / 行业
  [职场管理]  组织、管理、商业
  [个人生活]  生活、心理、文化

两条抓取通道
------------
1) pid 通道（推荐）：小宇宙网页版是 Next.js SSG，节目页 HTML 内嵌 __NEXT_DATA__，
   props.pageProps.podcast.episodes 含最近 15 集完整元数据（标题/时间/时长/
   shownotes/音频地址/是否付费/是否有官方文字稿）。免登录、免 token，一次 GET。
2) RSS 通道：OPML 里 xmlUrl 就是播客自己的 RSS。历史更长，但付费/独家集可能缺。

用法
----
  python3 xyz_tracker.py --import-opml ~/Downloads/subscriptions.opml  # 导入并自动解析 pid
  python3 xyz_tracker.py --list                                        # 查看当前配置
  python3 xyz_tracker.py --init                                        # 建基线
  python3 xyz_tracker.py                                               # 看新增
  python3 xyz_tracker.py --since 7d --report                           # 最近 7 天 + Markdown

实测踩坑记录（2026-09）
--------------------
- xiaoyuzhoufm.com/feed/{pid} → 404，不存在这个路由
- api.xiaoyuzhoufm.com 所有接口 → 401，需要 x-jike-access-token
- 逐字稿文本接口挂在主播后台（podcaster-api），监听端读不到
- 官方没有「我的订阅」聚合 RSS
- 小宇宙托管 feed（feed.xyzfm.space/<slug>）不声明 gzip 时是 1.5MB 明文，
  慢到会被超时截断；声明 gzip 后仅 ~325KB、1.5 秒。本脚本已处理。
- 托管 feed 全文含大量交叉推荐的别的播客链接，**不能用满文正则搜 pid**，
  只能取频道级 <link>（本脚本就是这么做的）。
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import csv
import gzip
import json
import random
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_WATCHLIST = HERE / "podcasts.txt"
DEFAULT_STATE = HERE / "state.json"
DEFAULT_REPORT_DIR = HERE / "reports"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
CST = timezone(timedelta(hours=8))
TIMEOUT = 60
# 每个来源只比对最新 N 期。追新场景够用，同时保证状态文件不会无限膨胀、
# 也避免「RSS feed 有几百期、状态被截断后老单集被反复当新增报」的 bug。
MAX_PER_SOURCE = 80
STATE_KEEP = 120
DOMAINS = ["专业领域", "职场管理", "个人生活"]
UNCLASSIFIED = "未分类"

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)
PID_RE = re.compile(r"[0-9a-fA-F]{24}")
PID_IN_URL_RE = re.compile(r"/(?:podcast|episode)/([0-9a-fA-F]{24})")
CHANNEL_LINK_RE = re.compile(r"<channel\b.*?<link>([^<]+)</link>", re.S)
ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"


def log(msg: str = "") -> None:
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------
# 播客背景档案（由 build_profiles.py 生成）
# --------------------------------------------------------------------------
PROFILES_CSV = HERE / "profiles.csv"

RISK_MARK = {"高": "⚠️", "中": "", "低": ""}


def load_profiles(path: Path = PROFILES_CSV) -> dict[str, dict]:
    """读 profiles.csv：key → {org, kind, risk, tip}。缺文件时返回空字典，不影响主流程。"""
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    try:
        with path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("key"):
                    out[row["key"]] = row
    except Exception as e:  # noqa: BLE001
        log(f"profiles.csv 读取失败（忽略）：{type(e).__name__}")
    return out


def fmt_profile(p: dict | None) -> str:
    """把档案压成一行注解，附在节目名后面。"""
    if not p:
        return ""
    org = (p.get("org") or "").strip()
    kind = (p.get("kind") or "").strip()
    risk = (p.get("risk") or "").strip()
    if not org and not kind:
        return ""
    bits = [b for b in (org, kind) if b]
    tail = f"·立场{risk}" if risk and risk != "低" else ""
    mark = RISK_MARK.get(risk, "")
    inner = " · ".join(bits)
    if tail:
        inner = f"{inner} {tail}"
    return f"  {mark}（{inner}）" if inner else ""


# --------------------------------------------------------------------------
# 人工评级（S / A / B），由 ratings.csv 维护
# --------------------------------------------------------------------------
RATINGS_CSV = HERE / "ratings.csv"
RATING_RANK = {"S": 0, "A": 1, "B": 3}  # 未评级 = 2（★ 未评级按 1.5 提升）


def load_ratings(path: Path = RATINGS_CSV) -> dict[str, str]:
    """读 ratings.csv 的「评级」列：key → S/A/B。"""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    try:
        with path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                r = (row.get("评级") or "").strip().upper()
                if row.get("key") and r in ("S", "A", "B"):
                    out[row["key"]] = r
    except Exception as e:  # noqa: BLE001
        log(f"ratings.csv 读取失败（忽略）：{type(e).__name__}")
    return out


def rank_of(item: dict) -> float:
    r = (item.get("rating") or "").strip().upper()
    if r in RATING_RANK:
        return RATING_RANK[r]
    return 1.5 if item.get("priority") else 2


# --------------------------------------------------------------------------
# HTTP：带 gzip / deflate 处理
# --------------------------------------------------------------------------
def http_get(url: str, timeout: int = TIMEOUT, retries: int = 4) -> str:
    """带 gzip/deflate 处理与退避重试。

    注意：部分播客主机（如 feeds.fireside.fm）在并发较高时会直接重置连接、
    抛 SSL UNEXPECTED_EOF。这不是持久故障，退避重试即可恢复；并发建议不超过 8。
    """
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Accept-Encoding": "gzip, deflate",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc:
                raw = gzip.decompress(raw)
            elif "deflate" in enc:
                try:
                    raw = zlib.decompress(raw)
                except zlib.error:
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            return raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 500, 502, 503):
                last = e
                time.sleep(2 + attempt * 3 + random.random())
                continue
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 + attempt * 2)
    raise last if last else RuntimeError("unknown fetch error")


# --------------------------------------------------------------------------
# pid 通道
# --------------------------------------------------------------------------
def fetch_pid(key: str) -> dict:
    try:
        html = http_get(f"https://www.xiaoyuzhoufm.com/podcast/{key}")
    except urllib.error.HTTPError as e:
        return {"ok": False, "kind": "pid", "key": key, "error": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "kind": "pid", "key": key, "error": f"{type(e).__name__}: {e}"}

    m = NEXT_DATA_RE.search(html)
    if not m:
        return {"ok": False, "kind": "pid", "key": key, "error": "页面结构变化：无 __NEXT_DATA__"}
    try:
        pod = json.loads(m.group(1))["props"]["pageProps"]["podcast"]
    except (KeyError, ValueError) as e:
        return {"ok": False, "kind": "pid", "key": key, "error": f"数据解析失败：{e}"}
    if not pod:
        return {"ok": False, "kind": "pid", "key": key, "error": "无 podcast 数据"}

    eps = []
    for ep in pod.get("episodes", []):
        if ep.get("isPrivateMedia"):
            continue
        eps.append({
            "id": ep.get("eid", ""),
            "title": (ep.get("title") or "").strip(),
            "pub": ep.get("pubDate") or "",
            "duration": ep.get("duration") or 0,
            "desc": ep.get("description") or ep.get("shownotes") or "",
            "url": f"https://www.xiaoyuzhoufm.com/episode/{ep.get('eid','')}",
            "pay": ep.get("payType") or "",
        })
        # 注意：不要用 episode.transcript.mediaId 判断「有没有文字稿」。
        # 实测 73/73 期该字段恒等于音频的 media.id，它只是「转写对应哪个音频」的指针，
        # 不含任何文字稿存在性信息。文字稿文本在主播后台，监听端读不到。
    return {
        "ok": True, "kind": "pid", "key": pod.get("pid", key),
        "title": pod.get("title") or key,
        "episode_count": pod.get("episodeCount"),
        "latest": pod.get("latestEpisodePubDate") or "",
        "brief": pod.get("brief") or "",
        "podcasters": [
            {"name": c.get("nickname", ""), "bio": (c.get("bio") or "").strip()}
            for c in (pod.get("podcasters") or [])
        ],
        "episodes": eps,
    }


# --------------------------------------------------------------------------
# RSS 通道
# --------------------------------------------------------------------------
def resolve_pid_from_feed(feed_text: str) -> str | None:
    """只取频道级 <link>，绝不全文正则（feed 里全是交叉推荐的别人的链接）。"""
    m = CHANNEL_LINK_RE.search(feed_text)
    if not m:
        return None
    hit = PID_IN_URL_RE.search(unescape(m.group(1).strip()))
    return hit.group(1) if hit else None


def _txt(node, *tags) -> str:
    for t in tags:
        el = node.find(t)
        if el is not None and el.text:
            return el.text.strip()
    return ""


def fetch_rss(key: str) -> dict:
    try:
        raw = http_get(key, retries=5)
    except urllib.error.HTTPError as e:
        return {"ok": False, "kind": "rss", "key": key, "error": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "kind": "rss", "key": key, "error": f"{type(e).__name__}: {e}"}
    try:
        root = ET.fromstring(raw.encode("utf-8", "replace"))
    except ET.ParseError as e:
        return {"ok": False, "kind": "rss", "key": key, "error": f"XML 解析失败：{e}"}
    chan = root.find("channel")
    if chan is None:
        return {"ok": False, "kind": "rss", "key": key, "error": "不是标准 RSS 2.0"}

    eps = []
    for it in chan.findall("item"):
        link = _txt(it, "link")
        guid = _txt(it, "guid") or link
        dur_raw = _txt(it, f"{{{ITUNES}}}duration")
        dur = 0
        if dur_raw:
            if ":" in dur_raw:
                for part in [x for x in dur_raw.split(":") if x.strip().isdigit()]:
                    dur = dur * 60 + int(part)
            elif dur_raw.isdigit():
                dur = int(dur_raw)
        eps.append({
            "id": guid or link, "title": _txt(it, "title"), "pub": _txt(it, "pubDate"),
            "duration": dur,
            "desc": _txt(it, "description") or _txt(it, f"{{{CONTENT_NS}}}encoded"),
            "url": link, "pay": "",
        })
    return {
        "ok": True, "kind": "rss", "key": key, "title": _txt(chan, "title") or key,
        "episode_count": len(eps), "latest": eps[0]["pub"] if eps else "", "episodes": eps,
    }


# --------------------------------------------------------------------------
# 清单（带领域）
# --------------------------------------------------------------------------
def parse_source(raw: str) -> tuple[str, str] | None:
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return None
    if raw.startswith("http"):
        hit = PID_IN_URL_RE.search(raw)
        if hit and "xiaoyuzhoufm.com" in raw:
            return ("pid", hit.group(1))
        if "xiaoyuzhoufm.com" in raw:
            return None
        return ("rss", raw)
    if PID_RE.fullmatch(raw):
        return ("pid", raw)
    return None


def parse_config(path: Path) -> tuple[list[str], dict[str, list[list[str]]], list[str]]:
    """解析配置文件 → (顶部注释, {领域: [[原始行, key, note], ...]}, 领域顺序)

    注意：行内以 # 之后的内容视为备注，所以 RSS 链接不要带 URL fragment。
    """
    comments: list[str] = []
    sections: dict[str, list[list[str]]] = {}
    order: list[str] = []
    if not path.exists():
        return comments, sections, order

    domain = UNCLASSIFIED
    seen_domain = False
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            domain = s[1:-1].strip() or UNCLASSIFIED
            seen_domain = True
            if domain not in sections:
                sections[domain] = []
                order.append(domain)
            continue
        if not s:
            continue
        if not seen_domain:
            # 第一个分节之前的内容都算文件头注释
            comments.append(line)
            continue
        if domain not in sections:
            sections[domain] = []
            order.append(domain)
        if s.startswith("#"):
            sections[domain].append([line, "", ""])
            continue
        body, note = (s.split("#", 1) if "#" in s else (s, ""))
        src = parse_source(body.strip())
        if not src:
            sections[domain].append([line, "", ""])
            continue
        sections[domain].append([line, src[1], note.strip()])

    if not comments:
        comments = ["# 小宇宙播客监控清单｜按 [领域] 分节，每行 pid 或 RSS 链接，行尾可写 # 备注"]
    return comments, sections, order


def dump_config(path: Path, comments: list[str], sections: dict, order: list[str]) -> None:
    lines = list(comments) + [""]
    for dom in order:
        rows = sections.get(dom) or []
        lines.append(f"[{dom}]")
        for raw, key, note in rows:
            lines.append(raw)
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def load_watchlist(path: Path) -> list[dict]:
    """支持 [领域] 分节；无分节归入未分类。

    备注以 ★ 开头表示「重点关注」：在同一领域内排到最前并加星标记。
    """
    _, sections, order = parse_config(path)
    out: list[dict] = []
    seen: set[str] = set()
    for dom in order:
        for raw, key, note in sections.get(dom, []):
            if not key or key in seen:
                continue
            seen.add(key)
            kind = "rss" if raw.strip().startswith("http") else "pid"
            prio = note.startswith("★")
            out.append({
                "domain": dom, "kind": kind, "key": key,
                "note": note.lstrip("★").strip(), "priority": prio,
            })
    return out


def import_opml(opml_paths, watchlist: Path, concurrency: int = 8) -> int:
    """导入 OPML（可传多个文件）：能解析出 pid 的走 pid 通道，否则退回 RSS 通道。"""
    if isinstance(opml_paths, (str, Path)):
        opml_paths = [opml_paths]

    outlines: list[tuple[str, str]] = []
    seen_feed: set[str] = set()
    for p in opml_paths:
        p = Path(p)
        if not p.exists():
            log(f"跳过（不存在）：{p}")
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"<outline\b[^>]*>", text):
            tag = m.group(0)
            um = re.search(r'xmlUrl="([^"]+)"', tag)
            if not um:
                continue
            feed = unescape(um.group(1))
            key = feed.rstrip("/").lower()
            if key in seen_feed:
                continue
            seen_feed.add(key)
            tm = re.search(r'\b(?:title|text)="([^"]*)"', tag)
            name = unescape(tm.group(1)).split("\n")[0].strip() if tm else feed
            outlines.append((name, feed))

    if not outlines:
        log("OPML 里没解析到任何播客。")
        return 1

    log(f"去重后 {len(outlines)} 个播客，正在解析 pid（需逐个拉 feed，并发 {concurrency}）…")

    def resolve(item: tuple[str, str]) -> tuple[str, str, str]:
        name, feed = item
        hit = re.search(r"/([0-9a-fA-F]{24})(?:\.xml)?/?$", feed)
        if hit:
            return (name, hit.group(1), "pid")
        try:
            pid = resolve_pid_from_feed(http_get(feed, timeout=60, retries=1))
        except Exception:  # noqa: BLE001
            pid = None
        return (name, pid, "pid") if pid else (name, feed, "rss")

    rows: list[tuple[str, str, str]] = []
    with futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        for i, (name, key, kind) in enumerate(ex.map(resolve, outlines), 1):
            rows.append((name, key, kind))
            log(f"  [{i}/{len(outlines)}] {kind}  {name}  → {key}")
    got = sum(1 for _, _, k in rows if k == "pid")
    log(f"pid 通道 {got} 个 / RSS 通道 {len(rows) - got} 个")

    existing = load_watchlist(watchlist)
    have = {r["key"] for r in existing}
    added = [r for r in rows if r[1] not in have]

    head = ["# 小宇宙播客监控清单｜按 [领域] 分节，每行 pid 或 RSS 链接，行尾可写 # 备注"]
    if watchlist.exists():
        head += [ln for ln in watchlist.read_text(encoding="utf-8").splitlines()
                 if ln.startswith("#")]

    by_dom: dict[str, list[tuple[str, str, str]]] = {}
    for name, key, kind in added:
        by_dom.setdefault(UNCLASSIFIED, []).append((name, key, kind))

    lines = head + [""]
    for d in DOMAINS + [UNCLASSIFIED]:
        items = by_dom.get(d) or []
        if not items:
            continue
        lines.append(f"[{d}]")
        for name, key, kind in items:
            lines.append(f"{key}   # {name}")
        lines.append("")
    watchlist.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    log(f"新导入 {len(added)} 个，跳过重复 {len(rows) - len(added)} 个")
    log(f"默认放在 [未分类]，请打开 {watchlist} 挪到 [专业领域] / [职场管理] / [个人生活] 下。")
    return 0


def itunes_search(name: str) -> list[tuple[str, str]]:
    """用 iTunes 搜索接口按名称找到播客的 RSS 地址。"""
    from urllib.parse import urlencode
    url = "https://itunes.apple.com/search?" + urlencode(
        {"term": name, "entity": "podcast", "country": "CN", "limit": 3}
    )
    try:
        data = json.loads(http_get(url, timeout=20, retries=2))
    except Exception:  # noqa: BLE001
        return []
    return [(r.get("collectionName", ""), r.get("feedUrl", ""))
            for r in data.get("results", []) if r.get("feedUrl")]


def resolve_by_name(name: str) -> tuple[str, str, str]:
    """名称 → (kind, key, 实际标题)。优先拿 pid，拿不到退回 RSS。"""
    hits = itunes_search(name)
    for title, feed in hits[:2]:
        try:
            pid = resolve_pid_from_feed(http_get(feed, timeout=40, retries=1))
        except Exception:  # noqa: BLE001
            pid = None
        if pid:
            return "pid", pid, title
    if hits:
        title, feed = hits[0]
        return "rss", feed, title
    return "", "", ""


def cmd_add(names: list[str], domain: str, watchlist: Path) -> int:
    comments, sections, order = parse_config(watchlist)
    if domain not in sections:
        sections[domain] = []
        order.append(domain)
    have = {k for dom in sections for _, k, _ in sections[dom] if k}

    added = 0
    for name in names:
        kind, key, title = resolve_by_name(name)
        if not key:
            log(f"  未找到：{name}")
            continue
        if key in have:
            log(f"  已存在，跳过：{title or name}")
            continue
        have.add(key)
        sections[domain].append([f"{key}   # {title or name}", key, title or name])
        added += 1
        log(f"  + [{domain}] {title or name}  ({kind})  {key}")

    dump_config(watchlist, comments, sections, order)
    log(f"新增 {added} 个 → {watchlist}")
    return 0 if added else 1


# --------------------------------------------------------------------------
# 增量
# --------------------------------------------------------------------------
def fetch_all(sources: list[dict], concurrency: int = 5,
              profiles: dict | None = None, ratings: dict | None = None) -> list[dict]:
    results: list[dict] = []
    with futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futs = {ex.submit(fetch_pid if s["kind"] == "pid" else fetch_rss, s["key"]): s
                for s in sources}
        for i, f in enumerate(futures.as_completed(futs), 1):
            s = futs[f]
            r = f.result()
            r["domain"] = s["domain"]
            r["note"] = s.get("note", "")
            r["priority"] = s.get("priority", False)
            r["profile"] = (profiles or {}).get(s["key"])
            r["rating"] = (ratings or {}).get(s["key"], "")
            results.append(r)
            log(f"  [{i}/{len(sources)}] {'OK ' if r['ok'] else 'ERR'} {r.get('title') or r['key']}")
    return results


def to_dt(v: str) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(CST)
    except ValueError:
        pass
    try:
        d = parsedate_to_datetime(v)
        return d.astimezone(CST) if d.tzinfo else d.replace(tzinfo=CST)
    except (TypeError, ValueError):
        return None


def fmt_dur(sec) -> str:
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return "-"
    if sec <= 0:
        return "-"
    h, rem = divmod(sec, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def clean(text: str, limit: int) -> str:
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", " ", unescape(text))
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit] + ("…" if len(t) > limit else "")


def newest_episodes(eps: list[dict], limit: int = MAX_PER_SOURCE) -> list[dict]:
    """只保留最新 limit 期，保证 diff 与状态都在有界窗口内。"""
    if len(eps) <= limit:
        return eps
    return sorted(
        eps, key=lambda e: to_dt(e["pub"]) or datetime.min.replace(tzinfo=CST), reverse=True
    )[:limit]


def collect_new(results: list[dict], state: dict, since: datetime | None,
                limit: int = MAX_PER_SOURCE) -> list[dict]:
    items = []
    for r in results:
        if not r["ok"]:
            continue
        seen = set(state.get(r["key"], []))
        for ep in newest_episodes(r["episodes"], limit):
            if ep["id"] in seen:
                continue
            dt = to_dt(ep["pub"])
            if since and dt and dt < since:
                continue
            items.append({
                "domain": r["domain"], "podcast": r["title"], "source": r["kind"],
                "key": r["key"], "id": ep["id"], "title": ep["title"], "pub_dt": dt,
                "duration": fmt_dur(ep["duration"]), "duration_sec": ep["duration"] or 0,
                "pay": ep["pay"], "desc": clean(ep["desc"], 160), "url": ep["url"],
                "priority": r.get("priority", False),
                "profile": r.get("profile"),
                "rating": r.get("rating", ""),
            })
    items.sort(key=lambda x: x["pub_dt"] or datetime.min.replace(tzinfo=CST), reverse=True)

    # 去重：同一期被多个播客联合发布时只留一条（保留 ★ 的那条），并记录联合方
    dedup: dict[tuple, dict] = {}
    for it in items:
        norm = re.sub(r"[\s\W_]+", "", it["title"]).lower()
        day = it["pub_dt"].date() if it["pub_dt"] else None
        k = (norm, day)
        prev = dedup.get(k)
        if prev is None:
            dedup[k] = it
            continue
        if it["priority"] and not prev["priority"]:
            it["also_in"] = [prev["podcast"]] + prev.get("also_in", [])
            dedup[k] = it
        else:
            prev.setdefault("also_in", []).append(it["podcast"])
    items = sorted(dedup.values(),
                   key=lambda x: x["pub_dt"] or datetime.min.replace(tzinfo=CST), reverse=True)
    return items


def dom_group(items: list[dict], dom: str) -> list[dict]:
    """同一领域内：先按人工评级（S > A > 未评 > B，★ 未评提一级），再按时间倒序。"""
    g = [i for i in items if i["domain"] == dom]
    g.sort(key=lambda x: (rank_of(x),
                          -(x["pub_dt"].timestamp() if x["pub_dt"] else 0)))
    return g


def rating_tag(item: dict) -> str:
    r = (item.get("rating") or "").strip().upper()
    return f"[{r}] " if r in ("S", "A", "B") else ""


def update_state(results: list[dict], state: dict,
                 limit: int = MAX_PER_SOURCE) -> dict:
    for r in results:
        if not r["ok"]:
            continue
        fresh = [e["id"] for e in newest_episodes(r["episodes"], limit)]
        seen = list(dict.fromkeys(state.get(r["key"], []) + fresh))
        state[r["key"]] = seen[-STATE_KEEP:]
    return state


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------
def print_console(items: list[dict], stamp: str, max_per_pod: int = 0) -> None:
    if not items:
        print(f"[{stamp}] 没有新更新。")
        return
    prio_n = sum(1 for i in items if i.get("priority"))
    head = f"[{stamp}] 发现 {len(items)} 期新内容"
    if prio_n:
        head += f"，其中重点关注 {prio_n} 期"
    print(head + "\n")
    for dom in DOMAINS + [UNCLASSIFIED]:
        group = dom_group(items, dom)
        if not group:
            continue
        print(f"━━━ {dom} ━━━")
        # 先按节目归拢，保证一档只出现一次（按评级/时间排序后同节目的单集不连续）
        order: list[str] = []
        by_pod: dict[str, list[dict]] = {}
        for it in group:
            if it["podcast"] not in by_pod:
                by_pod[it["podcast"]] = []
                order.append(it["podcast"])
            by_pod[it["podcast"]].append(it)
        for pod in order:
            eps = by_pod[pod]
            first = eps[0]
            star = "★ " if first.get("priority") else ""
            shown = eps[:max_per_pod] if max_per_pod and len(eps) > max_per_pod else eps
            extra = f"（共 {len(eps)} 期，只列最近 {len(shown)} 期）" if len(shown) < len(eps) else ""
            print(f"{star}{rating_tag(first)}● {pod}{fmt_profile(first.get('profile'))}{extra}")
            for it in shown:
                when = it["pub_dt"].strftime("%m-%d %H:%M") if it["pub_dt"] else "-"
                also = f"（与 {'、'.join(it['also_in'])} 联合发布）" if it.get("also_in") else ""
                print(f"   - {it['title']}")
                print(f"     {when} · {it['duration']}{also}")
                print(f"     {it['url']}")
                if it["desc"]:
                    print(f"     {it['desc']}")
        print()
    print(f"合计时长约 {sum(i['duration_sec'] for i in items) / 60:.0f} 分钟。")


def render_markdown(items: list[dict], results: list[dict], stamp: str,
                    since_label: str, max_per_pod: int = 0) -> str:
    ok = [r for r in results if r["ok"]]
    fail = [r for r in results if not r["ok"]]
    L = [f"# 播客更新清单 · {stamp}", ""]
    L.append(f"- 监控：{len(ok)} 个成功 / {len(fail)} 个失败"
             + (f"（时间窗 {since_label}）" if since_label else ""))
    prio_n = sum(1 for i in items if i.get("priority"))
    L.append(f"- 新增：**{len(items)}** 期"
             + (f"，其中重点关注 {prio_n} 期" if prio_n else "")
             + (f"，合计约 {sum(i['duration_sec'] for i in items) / 60:.0f} 分钟" if items else ""))
    if items:
        kinds = Counter((i.get("profile") or {}).get("kind") or "未收录" for i in items)
        risks = Counter((i.get("profile") or {}).get("risk") or "" for i in items)
        ratings = Counter((i.get("rating") or "未评级") for i in items)
        L.append("- 内容性质：" + " / ".join(f"{k} {v} 期" for k, v in kinds.most_common()))
        L.append("- 你的评级：" + " / ".join(f"{k} {v} 期" for k, v in ratings.most_common()))
        hi = risks.get("高", 0)
        if hi:
            L.append(f"- 其中 **{hi} 期来自机构自营号**（⚠️ 建议按 PR 视角复核）")
    for dom in DOMAINS + [UNCLASSIFIED]:
        group = dom_group(items, dom)
        if not group:
            continue
        L += ["", f"## {dom}", ""]
        by_pod: dict[str, list[dict]] = {}
        for it in group:
            by_pod.setdefault(it["podcast"], []).append(it)
        for pod, eps in by_pod.items():
            star = "★ " if eps[0].get("priority") else ""
            L.append(f"### {star}{rating_tag(eps[0])}{pod}")
            shown = eps[:max_per_pod] if max_per_pod and len(eps) > max_per_pod else eps
            if len(shown) < len(eps):
                L.append("")
                L.append(f"_{len(eps) - len(shown)} 期未列出（本期共 {len(eps)} 期，只列最近 {len(shown)} 期）_")
            pr = eps[0].get("profile")
            if pr and (pr.get("org") or pr.get("kind")):
                bits = [b for b in (pr.get("org"), pr.get("kind")) if b]
                if pr.get("risk") and pr["risk"] != "低":
                    bits.append(f"立场{pr['risk']}")
                note = " · ".join(bits)
                if pr.get("tip"):
                    note += f" — {pr['tip']}"
                L.append("")
                L.append(f"> {note}")
            L.append("")
            for e in shown:
                when = e["pub_dt"].strftime("%m-%d %H:%M") if e["pub_dt"] else "-"
                flags = []
                if e["pay"] and e["pay"] != "FREE":
                    flags.append(e["pay"])
                flag = f" `{' / '.join(flags)}`" if flags else ""
                also = f"（与 {'、'.join(e['also_in'])} 联合发布）" if e.get("also_in") else ""
                L.append(f"- **{e['title']}**{flag}{also}")
                L.append(f"  - {when} · {e['duration']} · [打开]({e['url']})")
                if e["desc"]:
                    L.append(f"  - {e['desc']}")
            L.append("")
    if fail:
        L += ["## 抓取失败", ""]
        for r in fail:
            L.append(f"- `{r['key']}` — {r.get('error')}")
        L.append("")
    return "\n".join(L)


def cmd_list(path: Path) -> int:
    rows = load_watchlist(path)
    if not rows:
        log(f"清单为空：{path}")
        return 1
    print(f"配置来源：{path}")
    print(f"共 {len(rows)} 个\n")
    for dom in DOMAINS + [UNCLASSIFIED]:
        g = [r for r in rows if r["domain"] == dom]
        if not g:
            continue
        print(f"[{dom}]  {len(g)} 个")
        for r in g:
            print(f"  {'pid' if r['kind'] == 'pid' else 'rss'}  {r['key']}   {r['note']}")
        print()
    return 0


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="小宇宙播客更新追踪器（按生活领域分组）")
    ap.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST)
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--import-opml", nargs="+", type=Path, metavar="文件",
                    help="导入一个或多个 OPML 并自动解析 pid")
    ap.add_argument("--add", nargs="+", metavar="名称", help="按名称添加播客（自动解析 pid）")
    ap.add_argument("--add-domain", default=UNCLASSIFIED,
                    help=f"配合 --add 使用，目标领域，默认 {UNCLASSIFIED}")
    ap.add_argument("--list", action="store_true", help="查看当前配置")
    ap.add_argument("--init", action="store_true", help="只建基线，不输出新增")
    ap.add_argument("--since", default=None, help="时间窗，如 7d / 24h / 30m")
    ap.add_argument("--domain", default=None, help="只跑某个领域")
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--max-per-source", type=int, default=MAX_PER_SOURCE,
                    help=f"每个来源只比对最新 N 期，默认 {MAX_PER_SOURCE}")
    ap.add_argument("--max-per-podcast", type=int, default=5,
                    help="每档最多列几期，0 表示不限；默认 5，避免日更节目刷屏")
    ap.add_argument("--report", action="store_true", help="写 Markdown 报告")
    ap.add_argument("--json", action="store_true", help="额外输出 JSON")
    ap.add_argument("--fail-on-error", action="store_true")
    args = ap.parse_args()

    if args.import_opml:
        missing = [p for p in args.import_opml if not p.exists()]
        if missing:
            log(f"找不到 OPML：{missing}")
            return 1
        return import_opml(args.import_opml, args.watchlist, args.concurrency)

    if args.add:
        return cmd_add(args.add, args.add_domain, args.watchlist)

    if args.list:
        return cmd_list(args.watchlist)

    sources = load_watchlist(args.watchlist)
    if args.domain:
        sources = [s for s in sources if s["domain"] == args.domain]
    if not sources:
        log(
            f"清单为空：{args.watchlist}\n\n"
            "小宇宙 App → 个人 → 设置 → 更多功能 → 导入或导出订阅列表 → 下载 OPML，然后：\n"
            "  python3 xyz_tracker.py --import-opml ~/Downloads/subscriptions.opml"
        )
        return 1

    since_dt, since_label = None, ""
    if args.since:
        m = re.fullmatch(r"(\d+)([dhm])", args.since.lower())
        if not m:
            log("--since 格式应为 7d / 24h / 30m")
            return 1
        n, unit = int(m.group(1)), m.group(2)
        since_dt = datetime.now(CST) - {
            "d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]
        since_label = args.since

    log(f"监控 {len(sources)} 个来源…")
    results = fetch_all(sources, args.concurrency, load_profiles(), load_ratings())

    state: dict = {}
    if args.state.exists():
        try:
            state = json.loads(args.state.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("状态文件损坏，已重建")

    if args.init:
        update_state(results, state, args.max_per_source)
        args.state.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        ok = sum(1 for r in results if r["ok"])
        log(f"基线已建立：{ok}/{len(results)} 个来源，记录 {sum(len(v) for v in state.values())} 期。"
            "此后运行只报新增。")
        return 0

    items = collect_new(results, state, since_dt, args.max_per_source)
    update_state(results, state, args.max_per_source)
    args.state.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    stamp = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    # --json 时 stdout 只放纯 JSON，便于直接管道给下游
    if not args.json:
        print_console(items, stamp, args.max_per_podcast)

    if args.report:
        DEFAULT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
        out = DEFAULT_REPORT_DIR / f"podcast-{datetime.now(CST):%Y-%m-%d}.md"
        out.write_text(render_markdown(items, results, stamp, since_label,
                                       args.max_per_podcast), encoding="utf-8")
        log(f"报告已写入：{out}")

    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2, default=str))

    if args.fail_on_error and any(not r["ok"] for r in results):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

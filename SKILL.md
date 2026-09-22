---
name: xyz-podcast-tracker
description: 扫描小宇宙（Xiaoyuzhou FM）播客更新，按工作目的（专业领域 / 职场管理 / 个人生活）分类，把新单集链接推送进「得到大脑」（Get 笔记）自动转写与摘要，并输出在标题前标注 S / A / B 优先级的情报清单。当用户说「扫一下小宇宙」「播客有什么更新」「播客情报」「灌进得到大脑」「本周播客」「掌握了哪些信息源」时使用。
agent_created: true
---

# 小宇宙播客情报管线

把「我关注的播客更新了什么」变成一份**按工作目的分类、带 S/A/B 优先级、已灌进得到大脑可随时提问**的情报清单。

**核心链路**：扫描更新 → 按评级筛选 → 推送进得到大脑（自动转写+摘要）→ 输出情报清单。

---

## 何时使用

- 用户问「小宇宙 / 播客有什么更新」「扫一下播客」「本周播客」
- 用户要把播客内容沉淀进得到大脑，方便随时提问
- 用户要维护信息源清单（背景、立场、评级）

## 一键流程（建议每周跑一次）

```bash
cd ~/.workbuddy/skills/xyz-podcast-tracker

# 1) 先看会推什么（不写入）
python3 push_to_getnote.py --since 7d --dry-run

# 2) 正式推送 S/A 级新单集进得到大脑
python3 push_to_getnote.py --since 7d

# 3) 若有超时条目，回查确认（关键：不要重新推送）
python3 push_to_getnote.py --verify
```

产出：`reports/podcast-digest-YYYY-MM-DD.md` —— 按领域分组、标题前带 `[S]`/`[A]`、
含「讲了什么」摘要、附得到大脑笔记链接。

维护类命令（改配置或加节目后才需要）：

```bash
python3 xyz_tracker.py --list                 # 看当前监控与分领域统计
python3 fetch_shows.py                        # 刷新实测数据（节奏/时长/订阅）
python3 build_profiles.py                     # 重新生成背景与立场分类
python3 build_roster.py                       # 生成评级清单（保留已填评级）
python3 build_namelist.py                     # 按 S/A/B 分层生成名单
```

---

## 小宇宙的三条通道（实测，别再重复试错）

| 通道 | 结果 |
|---|---|
| `xiaoyuzhoufm.com/feed/{pid}` | ❌ 404，不存在 |
| 官方「我的订阅」聚合 RSS | ❌ 不存在 |
| 网页搜索页 `/search?q=` | ❌ 404，无法按名称反查 |
| `api.xiaoyuzhoufm.com/*` | ❌ 401，需 `x-jike-access-token` |
| **节目页 `__NEXT_DATA__`** | ✅ **唯一可用的免登录通道** |

节目页是 Next.js SSG，HTML 内嵌 `<script id="__NEXT_DATA__">`，取
`props.pageProps.podcast.episodes` 得**最近 15 集**完整元数据（标题/发布时间/时长/
shownotes/音频地址/payType/`podcasters` 主播资料/`brief` 简介）。免登录、免 token。

### 四个必须知道的坑

1. **托管 feed 不带 gzip 会超时截断。** `feed.xyzfm.space/<slug>` 不声明
   `Accept-Encoding` 时返回 1.5MB 明文，慢到被截断（XML 报 `unclosed CDATA`）。
   声明 gzip 后仅 ~325KB。脚本已强制 gzip。
2. **托管 feed 全文含大量交叉推荐的别的播客链接。** 满文正则搜 `/podcast/<24hex>`
   会搜出十几个错 pid。**只能取频道级 `<link>`**。
3. **按名称反查 pid 会错配。** iTunes 搜「随机波动」返回的是「面基」。
   `--add` 之后必须 `--list` 核对。
4. **`transcriptMediaId` 不是文字稿信号。** 实测 73/73 期该字段恒等于音频自己的
   `media.id`，只是「转写对应哪个音频」的外键。小宇宙**没有开放逐字稿**。

---

## 得到大脑（Get 笔记）管线

### 为什么走得到大脑而不是自建 ASR

实测：`getnote save <小宇宙单集链接>` 后，得到大脑自动完成**抓音频 → 转写 → 摘要**。
69 分钟一期产出约 **3.6 万字带时间戳逐字稿** + **8 千字结构化 AI 摘要**。
无需本地模型、无需下载音频、无需任何 ASR 配置。

GitHub 上现成的小宇宙转录项目（`rrrrrredy/xiaoyuzhou-podcast`、
`lazywater-11/xiaoyuzhou-claude-skill`、`ychenjk-sudo/xiaoyuzhou-transcription-skill`、
`weisi-gu/xiaoyuzhou-podcast-notes`）**全都要自己接一个 ASR**，且都落到 Obsidian
或本地 md，不如得到大脑「能随时提问」。

### 两个必读操作坑

1. **CLI 约 30 秒超时报错，但服务端其实已经成功。**
   返回 `context deadline exceeded` / `retryable: false`，稍后 `getnote notes`
   就能查到。→ **超时后禁止重试**，跑 `--verify` 回查；盲目重试会产生重复笔记。
2. **`note transcript` 对链接类笔记不可用。** 逐字稿要用 `getnote note original` 取。

取内容：
```bash
getnote note <note_id> -o json            # content = AI 摘要
getnote note original <note_id> -o json   # original = 带时间戳逐字稿
```

配额实测极宽裕：write_note 1000 次/天、10000 次/月。

---

## 按工作目的分类

三个领域写在 `podcasts.txt` 的 `[领域]` 分节里，**这是领域的唯一准绳**：

| 领域 | 放什么 |
|---|---|
| `[专业领域]` | AI / 科技 / 创投 / 商业分析 —— 与投资工作直接相关 |
| `[职场管理]` | 组织、管理、职场、个人成长 |
| `[个人生活]` | 生活、心理、文化、阅读 |

归档到哪个得到大脑知识库由 `getnote.json` 决定：

```json
{
  "default_kb": "",
  "domain_kb": { "专业领域": "<kb_id>", "职场管理": "<kb_id>", "个人生活": "<kb_id>" },
  "ratings": ["S", "A"],
  "title_prefix": true,
  "exclude_kinds": ["二手工具号"]
}
```

- 知识库 id 用 `getnote kbs -o json` 查
- `exclude_kinds`：默认排除「二手工具号」（AI 翻译搬运类），避免污染知识库
- 用 `--kb` 可临时覆盖

## S / A / B 优先级

**评级由用户亲自打**，存在 `ratings.csv` 的「评级」列（S / A / B 三选一）。
用户在对话里说评级时，直接改该列。

- **推送的标题前会标注优先级**：`[S] 十字路口Crossing｜于是转身向具身走去`
  （`title_prefix: false` 可关闭，改回得到大脑自动扩写的标题）
- **日报排序**：同领域内 `S → A → 未评（★ 提一级）→ B`
- **默认只推 S / A** 级；`--ratings S` 可只推 S

解析用户在《播客清单与评级.md》里的手写评级时注意：用户是**在
`☐ S　☐ A　☐ B` 下面另起一行写等级**，不是勾选方框。

---

## 脚本链条

```
fetch_shows.py      抓实测数据（节奏/时长/订阅/简介/主播）→ show-cache.json，失败保留旧值
build_profiles.py   背景与立场分类 → profiles.csv + 播客背景与立场梳理.md
build_roster.py     生成评级清单 → 播客清单与评级.md + ratings.csv（保留已填评级）
build_namelist.py   按评级分层 → 评级名单.md（含 S 级时间成本）
xyz_tracker.py      扫描更新 → 按领域分组 + 评级排序 + 背景标注
push_to_getnote.py  【核心】推送进得到大脑 + 输出带 S/A/B 标注的情报清单
```

改了 `podcasts.txt` 后建议依次重跑 `build_profiles.py` → `build_roster.py`。

## 汇报格式

按领域分组，**S 优先**，每条给：`[S] 播客名｜单集标题` → 时间 → 时长 →
得到大脑笔记链接 → 「讲了什么」摘要。结尾给已入库期数与时间成本。
若某期来自**机构自营号**（立场高），主动提示按 PR 视角复核。

## 已知边界

- 接口无官方承诺，小宇宙改版后 `__NEXT_DATA__` 结构可能变；脚本会明确报
  「页面结构变化」而非静默出错。
- pid 通道每档只有最近 15 集，追新够用；完整历史需走 RSS 通道。
- 付费/私密单集（`isPrivateMedia`）会被跳过。
- 并发建议 ≤8：`feeds.fireside.fm` 在并发高时会重置连接抛
  `SSL UNEXPECTED_EOF_WHILE_READING`（退避重试可恢复，非持久故障）。

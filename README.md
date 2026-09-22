# 小宇宙 × 得到大脑 · 播客情报管线

> 把小宇宙（Xiaoyuzhou FM）的关注播客更新，变成一份**按工作目的分类、带 S/A/B 优先级、
> 已灌进笔记库可随时提问**的情报清单。
>
> Turn your Xiaoyuzhou (Chinese podcast platform) subscriptions into a prioritized,
> categorized intelligence digest — and push each episode into your note app for
> auto-transcription and Q&A.

零第三方依赖（仅 Python 标准库）。

---

## 它解决什么

关注的播客太多，一周产出几十期、上百小时，**根本听不完**，而你不知道该听哪几期。

这个工具把你从「听完所有」改成**三层漏斗**：

```
第 0 层  扫描更新
        直接读小宇宙节目页的元数据（免登录），和本地状态做 diff
        → 按你的 S/A/B 评级排序，只留下新的
                ↓
第 1 层  自动入库 + 转写（无需你做任何事）
        把单集链接推送到笔记库，由笔记服务完成「抓音频 → 转写 → 摘要」
        → 你就得到了带时间戳的完整逐字稿 + 结构化摘要，随时可提问
                ↓
第 2 层  读摘要，判断要不要深看
        → 只有你真正关心的那几期，才值得花时间
                ↓
第 3 层  亲自听（可选）
        笔记里已有时间戳，可定位到具体段落
```

## 快速开始

```bash
git clone <this-repo> && cd xiaoyuzhou-getnote

# 1) 准备监控清单（复制示例后填自己的）
cp podcasts.example.txt podcasts.txt
#    拿 pid：打开播客主页，URL 里 /podcast/ 后那 24 位
#    或者：小宇宙 App 导出 OPML 后批量导入
python3 xyz_tracker.py --import-opml ~/Downloads/subscriptions.opml

# 2) 建基线（只记录，不输出）
python3 xyz_tracker.py --init

# 3) 看更新
python3 xyz_tracker.py --since 7d --report
```

要用「推送到笔记库」这条链路，先装官方 CLI：

```bash
npm i -g @getnote/cli && getnote setup    # 得到大脑 / Get 笔记
cp getnote.example.json getnote.json      # 填 default_kb（用 getnote kbs 查）
python3 push_to_getnote.py --since 7d --dry-run   # 先看会推什么
python3 push_to_getnote.py --since 7d             # 正式推送
python3 push_to_getnote.py --verify               # 核实超时条目（重要，见下）
```

---

## 实测结论：小宇宙的通道现状

别再重复试错了，这些我都验过：

| 通道 | 结果 |
|---|---|
| `xiaoyuzhoufm.com/feed/{pid}` | ❌ 404，这个格式不存在 |
| 官方「我的订阅」聚合 RSS | ❌ 不存在 |
| 网页搜索页 `/search?q=` | ❌ 404，无法按名称反查 |
| `api.xiaoyuzhoufm.com/*` | ❌ 401，需要 `x-jike-access-token` |
| 逐字稿读取接口 | ❌ 全挂在主播后台（`/management/episode-transcript/*`） |
| **节目页 `__NEXT_DATA__`** | ✅ **唯一可用的免登录通道** |

节目页是 Next.js SSG，HTML 里内嵌 `<script id="__NEXT_DATA__">`，取
`props.pageProps.podcast.episodes` 就能拿到**最近 15 集**的完整元数据
（标题 / 发布时间 / 时长 / shownotes / 音频地址 / 是否付费 / 主播资料）。

### 四个坑（都踩过）

1. **托管 feed 不带 gzip 会超时截断。** `feed.xyzfm.space/<slug>` 不声明
   `Accept-Encoding` 时返回 1.5MB 明文，慢到被截断（XML 报 `unclosed CDATA`）。
   声明 gzip 后只有 ~325KB。脚本已强制 gzip。
2. **托管 feed 全文含大量交叉推荐的别人的链接。** 满文正则搜 `/podcast/<24hex>`
   会搜出十几个错 pid。**只能取频道级 `<link>`**。
3. **按名称反查 pid 会错配。** 用 iTunes 搜「随机波动」返回的是「面基」。
   `--add` 之后务必 `--list` 核对。
4. **`transcriptMediaId` 不是文字稿信号。** 实测 73/73 期该字段恒等于音频自己的
   `media.id`，只是「转写对应哪个音频」的外键。小宇宙**没有开放逐字稿**。

## 实测结论：为什么用笔记服务而不是自己跑 ASR

把一条小宇宙单集链接存进「得到大脑」，它会自动完成抓音频 → 转写 → 摘要。
69 分钟一期产出约 **3.6 万字带时间戳逐字稿** + **8 千字结构化摘要**，
**不需要本地模型、不需要下载音频、不需要任何 ASR 配置**。

GitHub 上其他小宇宙转录项目（`rrrrrredy/xiaoyuzhou-podcast`、
`lazywater-11/xiaoyuzhou-claude-skill`、`ychenjk-sudo/xiaoyuzhou-transcription-skill`、
`weisi-gu/xiaoyuzhou-podcast-notes`）**全都要自己接一个 ASR**，且都落到 Obsidian
或本地 md。除非你要离线可控或说话人分段，否则不如直接用它。

### 两个必读操作坑

1. **CLI 约 30 秒超时报错，但服务端其实已经成功。**
   会返回 `context deadline exceeded` / `retryable: false`，稍后就能查到笔记。
   → **超时后禁止重试**，跑 `--verify` 回查；盲目重试会产生重复笔记。
   实测 13 期中 9 期报超时，**最终 14/14 全部入库**。
2. **`getnote notes` 每页只返回 20 条**，必须按返回的 `cursor` 翻页，
   否则回查会漏掉较早创建的笔记。

---

## 脚本清单

| 脚本 | 作用 |
|---|---|
| `xyz_tracker.py` | 扫描更新、按领域分组、按评级排序、输出带背景标注的清单 |
| `push_to_getnote.py` | **核心**：推送单集到笔记库 + 输出带 `[S]`/`[A]` 标注的情报清单 |
| `fetch_shows.py` | 抓实测数据（更新节奏 / 平均时长 / 订阅数 / 简介 / 主播） |
| `build_profiles.py` | 生成信息源背景与立场分类（谁做的、有没有利益立场） |
| `build_roster.py` | 生成评级清单，供你打 S / A / B |
| `build_namelist.py` | 按 S/A/B 分层生成名单，含时间成本估算 |

## 按工作目的分类

领域写在 `podcasts.txt` 的 `[领域]` 分节里，**这是领域的唯一准绳**。
推荐三个，可按需增删：

| 领域 | 放什么 |
|---|---|
| `[专业领域]` | 与主业直接相关的技术 / 行业 / 商业分析 |
| `[职场管理]` | 组织、管理、职场、个人成长 |
| `[个人生活]` | 生活、心理、文化、阅读 |

推送时会按 `getnote.json` 的 `domain_kb` 归到不同笔记知识库。

## S / A / B 优先级

评级由你自己打，存在 `ratings.csv`。推送时**标题前会标注优先级**：

```
[S] 十字路口Crossing｜于是转身向具身思考走去
[A] 某播客｜某期标题
```

在笔记库里一眼就能扫出哪些是重点。`title_prefix: false` 可关闭，
改用笔记服务的自动扩写标题。

## 数据与隐私

`.gitignore` 默认排除所有个人数据（`podcasts.txt` / `ratings.csv` /
`profiles.csv` / `pushed.json` / `getnote.json` / `reports/`），
仓库里只保留 `*.example.*` 模板。**你的订阅列表和评级不会被提交。**

## 依赖

- Python 3.9+（仅标准库）
- 可选：`@getnote/cli`（笔记推送链路）

## License

MIT

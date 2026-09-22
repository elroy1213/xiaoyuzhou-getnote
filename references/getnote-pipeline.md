# 笔记库推送管线（得到大脑 / Get 笔记）

把一个播客单集的链接交给笔记服务，由它完成「抓音频 → 转写 → 摘要」。
本页记录实测行为与两个必须回避的坑。

## 实测行为

对 69 分钟的单集，`getnote save <小宇宙单集链接>` 之后产出：

| 产出 | 取法 | 大小 |
|---|---|---|
| AI 摘要（按主题分组的要点） | `getnote note <id> -o json` 的 `content` | 约 8 千字 |
| 逐字稿（**带时间戳**） | `getnote note original <id> -o json` 的 `original` | 约 3.6 万字 |

逐字稿开头是 `内容总时长:1小时9分钟`，正文形如：

```
[00:00 - 00:10] 主持人开场……
[00:36 - 00:41] 嘉宾回答……
```

笔记类型 `note_type = link`，会拿到一个 `note_url`，在笔记 App 里可直接对该期提问。

**所以不需要本地 ASR、不需要下载音频、不需要任何模型。**
本仓库自带 `whisper-cli` 之外的方案说明：若你要离线可控或说话人分段，
再考虑自建 ASR（见 README 里对比的四个开源项目）。

## 坑 1：CLI 约 30 秒超时报错，但服务端其实已经成功

`getnote save` 会返回：

```json
{"success": false,
 "error": {"code": -1,
           "message": "request failed: ... context deadline exceeded ...",
           "retryable": false}}
```

**但笔记往往已经创建成功。** 实测一次推 13 期，9 期报这个错，
稍后回查 **全部 14/14 都在库里**。

→ **超时后禁止重试。** 盲目重试会产生重复笔记。
正确做法是记录为 `pending`，再用 `getnote notes` 回查确认：

```bash
python3 push_to_getnote.py --verify
```

确认某些笔记**确实不存在**后，才用 `--forget` 移除记录以便重新推送。

## 坑 2：`getnote notes` 每页只返回 20 条

即使 `--limit` 传更大值，一页也只回 20 条。必须按返回的 `cursor` 翻页，
否则回查会漏掉较早创建的笔记（实测漏查率：不翻页时 20 条窗口之外全丢）。

```bash
getnote notes --limit 20 -o json            # 返回 has_more / cursor / total
getnote notes --cursor <cursor> -o json     # 翻下一页
```

## 其他

- `getnote note transcript <id>` 对 `link` 类笔记**不可用**，逐字稿走 `original`。
- 配额实测很宽裕：写笔记 1000 次/天、10000 次/月（`getnote quota` 查）。
- 标题统一加优先级前缀（`[S] 播客名｜单集标题`），在库里可直接按照优先级扫。

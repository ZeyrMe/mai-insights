# 麦麦见闻 · `zeyr.mai-insights`

> N + M 频道标签订阅路由、采集期零模型消耗攒料、滑动窗口整批提炼，以及严格物理隔离的“活人搬新鲜事”与“中立工具态总结”。

![Version](https://img.shields.io/badge/version-0.2.0-green)
![MaiBot](https://img.shields.io/badge/MaiBot-1.4.8%2B-blue)
![SDK](https://img.shields.io/badge/SDK-2.23.0-teal)
![License](https://img.shields.io/badge/License-MIT-lightgrey)

## 核心特性

- **N + M 标签订阅路由，终结 N×M 排列组合地狱**：
  - **源侧（`feeds` + `observe_groups`）**：RSS / RSSHub 订阅源和观察群只管给自己贴主题 `tags`（如 `tech`、`acg`、`gossip`）；
  - **去向侧（`targets`）**：每个群或显式私聊只管填写要 `subscribe` 的标签（`*` 全收）以及接收模式（`insight` / `digest` / `both`），新增源或新增群零耦合。
- **采集期零 LLM，定时/手动整批消化（告别逐条烧 token）**：
  - RSS 轮询（仅收当前逻辑日带发布时间的条目，支持多 RSSHub 镜像顺序降级）与观察群收消息（保留 `filter_humans` 过滤同行 Bot 噪音）**全程零大模型调用**，不逐条 `fsync`；
  - 攒料到消化间隔后，先按上限用 `better-search` 补充精选正文，再按条数/字数安全分段（Map）与分层归并（Reduce），一次产出**主题总览** + **0～3 条真正新鲜有趣的见闻**（仅允许真实原料 ID，且消化失败保留游标、跨日不混入新见闻池）。
- **活人分享 vs 工具总结严格隔离**：
  - **活人分享（`insight`）**：仅当目标群（或显式登记的私聊）近期有真人聊过天、且通过 `mai-day` 的 `open` 自由窗口与四阶段租约审批时，用当下会话人设自然搬运 1 条并拿到实际发送回执才推进冷却；
  - **工具总结（`digest`）**：`/总结` 与整点定时总结使用客观中立口吻生成，走服务授权发送且**绝不回写日常人设聊天历史**；`/总结 群内` 直接调用宿主 `ctx.message.get_by_time_in_chat` 拉取当天消息。
- **观察群绝对禁言红线**：观察群只进不出，绝不向观察群主动分享、绝不在观察群内回应 `/总结`。

---

## 安装与快速配置

将 `mai-insights/` 放入宿主 `plugins/` 目录并重启 MaiBot（宿主会自动安装 `feedparser` 与 `httpx`）。开源默认无任何订阅源与推送目标，开箱不会乱发消息。

`config.toml` 示例：

```toml
enabled = true
rsshub_base_urls = ["https://rsshub.example.com"]

[[feeds]]
id = "tech_daily"
url = "https://example.com/tech/rss.xml"
tags = ["tech"]

[[observe_groups]]
group = "111222333"
tags = ["gossip"]

[[targets]]
chat = "999888777"
subscribe = ["tech", "gossip"]
mode = "both"            # insight | digest | both

[[admins]]
platform = "qq"
user_id = "987654"
```

---

## 工具、命令、接口与 WebUI 控制台

### 对话工具（`@Tool`）
| 工具名 | 参数 | 作用 |
|---|---|---|
| `query_today_insights` | `topic=""` | 立即查看今天后台已经提炼好的精选见闻池（只读本地池，不现场联网） |

### 命令（需在 `admins` 或 `mai-circle.admins` 名单内）
| 命令格式 | 效果 |
|---|---|
| `/总结` 或 `/总结 全部` | 输出今日整批见闻与资讯汇总 |
| `/总结 全网` | 仅汇总今天来自 RSS / RSSHub 订阅源的内容 |
| `/总结 群内` | 汇总当前群当天逻辑日窗口的真实聊天内容（观察群自身禁发） |
| `/总结 群内 <群号>` | 在普通群或私聊内，按管理员权限远程查看指定群今日聊天总结 |

### 对外联动接口（`@API(..., version="1")`）
- `get_today_insights(limit=3, topic="")` / `mark_shared(item_id, stream_id, receipt_id)`

### WebUI 运行时控制台（`config_widgets`）
| 控件 | 类型 | 说明 |
|---|---|---|
| `运行状态与生效范围` | 实时状态 | 展示各 RSS/观察群最近采集健康度、待消化原料条数、今日精选见闻及 `[scope]` 状态 |
| `立即采集全部来源` | 一键按钮 | 手动触发一轮全部启用订阅源拉取 |
| `立即整批消化` | 一键按钮 | 忽略定时间隔，立即对未消化原料跑一轮整批提炼 |
| `清空今日分享冷却` | 二次确认按钮 | 重置今天各目标群/私聊的分享冷却计时 |

---

## 生态联动与注意事项

- **与 `mai-day` 联动**：摸鱼 `open` 时段才放行搬运，采用 `reserve_share` → `maisaka.reply.generate` → `authorize_share` → 真实送达回执结算 `settle_share` 并回写 `mai-day` 动态事件；`/总结` 发送走 30 秒精确服务授权，不受睡眠静默误杀。缺席 `mai-day` 时按自身默认清醒时段与冷却独立工作。
- **与 `better-search` & `mai-circle` 联动**：整批消化前优先调 `better-search.fetch` 补齐正文；观察群缓冲自动过滤 `mai-circle` 标记的同行 Bot 消息。

<details>
<summary>开发、门禁与安全审计（独立仓库维护）</summary>

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r .github/requirements-ci.txt
.venv/bin/python scripts/check.py
```

- 包含 `feedparser` / `httpx` 依赖一致性检查、整批分段 Map-Reduce 回归测试与 CI 安全审计。

</details>

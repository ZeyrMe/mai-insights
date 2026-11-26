<div align="center">

# 麦麦见闻 · `zeyr.mai-insights`

**像群友一样后台逛网攒新料，攒够一波一次消化：顺手在摸鱼时搬一两条新鲜事进群聊，定时或 `/总结` 再拉一份干净总账**

![Version](https://img.shields.io/badge/version-0.2.0-brightgreen)
![MaiBot](https://img.shields.io/badge/MaiBot-1.4.8%2B-2f81f7)
![SDK](https://img.shields.io/badge/SDK-2.23.0-0e8a16)
![Standalone](https://img.shields.io/badge/独立运行-支持-blue)

[为什么用它](#为什么用它) · [3 步快速开始](#3-步快速开始) · [配置项说明](#常用配置项说明) · [命令与控制台](#命令工具与控制台) · [FAQ](#常见疑问)

</div>

---

## 为什么用它

| 常见问题 | 传统做法的痛点 | **麦麦见闻（`zeyr.mai-insights`）** |
|---|---|---|
| RSS 和群一多配成死结 | 每个源和每个群手写一条定向映射，增减来源痛苦不堪 | **N + M 标签订阅**：源只贴主题标签，目标群只写要订阅哪些标签（`*` 全收） |
| 每条消息/每条推送逐条总结 | 90% 的水话接龙也逐条调用模型，烧 token 还制造平庸摘要 | **采集期零模型消耗**：静默攒料后整批分段提炼（Map-Reduce），一次产出总览 + 0～3 条今日精华 |
| 工具播报味和闲聊人设串味 | 群里像个新闻播音员“今日头条如下……” | **双轨严格隔离**：活人分享走人设改写与 `mai-day` 租约；`/总结` 和整点播报走客观工具态，不进日常聊天记忆 |
| 观察群意外暴露或抢答 | 采集情报的群自己突然冒泡发消息 | **观察群物理级只采不发**，甚至本地在观察群触发 `/总结` 也死锁禁发 |

---

## 3 步快速开始

### WebUI 上手（推荐）
1. 将 `mai-insights/` 放入宿主 `plugins/` 并重启，打开 **麦麦见闻** 配置页（开源默认不带任何源和目标，不会乱发消息）。
2. 在 **RSS 订阅源** 添加信息源或到 **观察群来源** 填上想潜水的 QQ 群号，各自贴个好记的主题标签（如 `tech`、`acg`）。
3. 在 **订阅目标** 填上接收群号（私聊需显式写 `private:QQ号`）并填上订阅的标签与模式（`insight` / `digest` / `both`）；之后随时可在顶部控制台点 **立即采集全部来源** 和 **立即整批消化** 查看精选效果。

<details>
<summary>手改 config.toml 示例（附完整注释）</summary>

```toml
enabled = true
rsshub_base_urls = ["https://rsshub.example.com"]    # 用 rsshub_route 时按顺序降级尝试的镜像地址

[[feeds]]
id = "tech_news"
url = "https://example.com/tech/rss.xml"
tags = ["tech"]                                      # 源侧贴标签

[[observe_groups]]
group = "111222333"                                  # 只潜水采集的观察群，死锁不外发
tags = ["gossip"]

[[targets]]
chat = "999888777"                                   # 目标群；私聊请显式写 private:QQ号
subscribe = ["tech", "gossip"]                       # 写 ["*"] 订阅全部标签
mode = "both"                                        # insight（闲时搬1条）| digest（定时总结）| both

[[admins]]
platform = "qq"
user_id = "987654"                                   # 有权用 /总结 命令；留空可自动继承群关系管理员
```

</details>

---

## 常用配置项说明

| 配置项 | 默认值 | 怎么填与效果说明 |
|---|---|---|
| `feeds` | `[]` | RSS/Atom/RSSHub 源；仅接收当前逻辑日带发布时间的条目，隔夜旧闻不进池 |
| `observe_groups` | `[]` | 观察群列表；自动过滤 `mai-circle` 标好的同行 Bot 发言，入站不逐条落盘 |
| `targets` | `[]` | 接收目标列表；私聊不填绝不主动推，支持 `exclude_sources` 屏蔽个别源 |
| `digest_interval_minutes` | `180` | 高级设置：攒到新材料后，隔多少分钟醒着整批消化一次（默认 3 小时） |
| `max_insights_per_digest` | `3` | 高级设置：单次整批消化最多挑出几条最值得聊的新鲜事进见闻池 |
| `summary_hours` | `[]` | 高级设置：每天整点向 `mode` 为 `digest` 或 `both` 的目标发总结的小时（0–23） |
| `max_enrich_per_digest` | `3` | 高级设置：消化前最多调用 `better-search` 为几条短摘要补全原文正文 |

---

## 命令、工具与控制台

### 群/私聊总结命令（需管理员权限，观察群内原地禁发）
| 命令格式 | 作用 |
|---|---|
| `/总结` 或 `/总结 全部` | 输出今日已汇总的全网 + 观察群总览与精选清单 |
| `/总结 全网` | 仅汇总今天来自 RSS / RSSHub 订阅源的资讯 |
| `/总结 群内` | 拉取当前群今天逻辑日内的真实消息记录做整批工具总结 |
| `/总结 群内 <群号>` | 在普通群或私聊远程查看指定群的今日群内总结 |

### 工具与 WebUI 控制台
- **工具 `query_today_insights(topic="")`**：Bot 自己翻看今日已整批提炼的见闻池（只读不临时联网）。
- **WebUI 控制台**：`运行状态与生效范围`（源健康度、待消化数、今日精选预览）+ `立即采集全部来源` + `立即整批消化` + `清空今日分享冷却`。

---

## 生态联动与常见疑问

- **联动关系**：装有 `mai-day` 时必须拿到 `open` 自由窗租约并确认实际送达才推进共享冷却、顺便写入一条动态事件；未装 `mai-day` 时使用内置 `fallback_open_start_hour`（9:00–23:00）独立工作。
- **Q：如果群里水了一天几千条消息，整批消化会不会超出模型上下文？**
  不会。插件会先按条数与字数阈值拆成多块分段提炼（Map），再层级归并（Reduce）；如果某轮生成报错会保留原游标，下次重试绝不漏掉未消化原料。

<details>
<summary>开发、门禁与安全审计</summary>

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r .github/requirements-ci.txt
.venv/bin/python scripts/check.py
```

</details>

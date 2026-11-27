# 麦麦见闻（mai-insights）

订阅资讯与群聊话题，支持主动分享与聊天总结

![Version](https://img.shields.io/badge/version-0.2.0-brightgreen)
![MaiBot](https://img.shields.io/badge/MaiBot-1.4.8%2B-2f81f7)
![SDK](https://img.shields.io/badge/SDK-2.23.0-0e8a16)
![Python](https://img.shields.io/badge/Python-3.12%2B-3776ab)
![Standalone](https://img.shields.io/badge/独立运行-支持-blue)
![License](https://img.shields.io/badge/License-MIT-lightgrey)

## 功能介绍

- **标签化订阅分发**：RSS 订阅源与观察群分别标注主题标签，接收目标按标签自由订阅（支持 `*` 全选）与排除来源。
- **整批整理节省开销**：后台采集和去重不调用模型，积累新内容后按周期分批汇总（Map-Reduce），产出总览和 0~3 条精选见闻。
- **分享与总结分离**：`insight` 模式按当前会话人设在空闲时分享 1 条见闻；`digest` 定时总结和 `/总结` 命令保持客观条理，不混入日常聊天记忆。
- **观察群严格静音**：纳入观察群列表的群聊只采集内容，禁止在该群主动发言或回复总结。

## 效果示例

- **空闲话题分享**：后台定时从订阅的 RSS 源与观察群整理出当日精选；在群聊空闲且近期有人活跃时，挑 1 条用平时人设聊起。
- **客观聊天总结**：管理员发送 `/总结 群内` 或到整点向 `digest` 目标推送时，直接输出条理清晰的当天要点列表，不混入闲聊语气或日常记忆。

## 如何使用

1. 将 `mai-insights/` 放入宿主 `plugins/` 目录并重启，打开 **麦麦见闻** 配置页。
2. 在 **RSS 订阅源** 或 **观察群来源** 中添加信息来源并打上主题标签（如 `tech`、`acg`）。
3. 在 **订阅目标** 中填写接收群号（私聊写 `private:QQ号`）、订阅标签与模式（`insight` / `digest` / `both`）；之后可在顶部点击 **立即采集全部来源** 与 **立即整理汇总** 查看效果。

<details>
<summary>config.toml 配置示例</summary>

```toml
enabled = true
rsshub_base_urls = ["https://rsshub.example.com"]

[[feeds]]
id = "tech_news"
url = "https://example.com/tech/rss.xml"
tags = ["tech"]

[[observe_groups]]
group = "111222333"
tags = ["gossip"]

[[targets]]
chat = "999888777"
subscribe = ["tech", "gossip"]
mode = "both"

[[admins]]
platform = "qq"
user_id = "987654"
```

</details>

## 配置说明

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `feeds` | `[]` | RSS / Atom / RSSHub 订阅源配置 |
| `observe_groups` | `[]` | 只采集话题、绝不发言的观察群列表（自动跳过已知其他 Bot 消息） |
| `targets` | `[]` | 接收推送的目标群或显式私聊配置，支持按标签订阅与排除来源 |
| `digest_interval_minutes` | `180` | 高级设置：有新内容时执行一次整批汇总的间隔分钟数 |
| `max_insights_per_digest` | `3` | 高级设置：每次整批整理最多选出几条用于空闲分享 |
| `summary_hours` | `[]` | 高级设置：每日定时向 `digest`/`both` 目标发送总结的整点小时（0-23） |
| `max_enrich_per_digest` | `3` | 高级设置：整理前最多调用 `better-search` 抓取几条链接原文 |

## 命令与工具

| 类别 | 名称 / 格式 | 说明 |
|---|---|---|
| 管理命令 | `/总结` 或 `/总结 全部` | 输出今日已整理的全部来源总览（需管理员，观察群禁发） |
| 管理命令 | `/总结 全网` | 仅汇总今日来自 RSS 订阅源的内容 |
| 管理命令 | `/总结 群内 [群号]` | 拉取当前群（或私聊中指定群号）今日聊天记录做要点总结 |
| 对话工具 | `query_today_insights(topic="")` | 让模型查询今天已经整理好的资讯与群话题 |
| WebUI 控件 | `立即采集全部来源` / `立即整理汇总` / `清空今日分享冷却` | 手动触发拉取、汇总并查看各订阅源健康状态 |

<details>
<summary>开发与自检</summary>

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r .github/requirements-ci.txt
.venv/bin/python scripts/check.py
```

</details>

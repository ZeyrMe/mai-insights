# 更新日志 · 麦麦见闻 (`zeyr.mai-insights`)

本日志遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 规范。

## [0.2.0] - 2026-11-27

### 新增
- **N + M 标签订阅路由**：新增 `feeds.tags`、`observe_groups`、`rsshub_base_urls` 多实例降级与 `targets` 订阅规则，支持 `insight` / `digest` / `both` 三模式及显式私聊投递。
- **零 LLM 攒料 + 整批分段消化**：废弃逐条模型总结，采集期零模型消耗、不逐条落盘；消化期先按上限（默认每批 3 条）调用 `better-search.fetch` 补齐正文，再按条数/字数分段提炼并分层归并，仅真实原料可成为当日精选，失败保留原料游标、跨日不混新池。
- **独立范围 `[scope]` 与 WebUI 控制台**：新增范围卡片与“立即采集全部来源”“立即整批消化”“清空今日分享冷却”操作按钮，展示各源实时健康状态。

### 优化
- **WebUI 与文案优化**：隐藏旧版 `routes`、`passive_observation_groups`、`prepare_per_tick`、`insight_instruction`、`summary_targets` 与底层缓存/超时阈值；全部可见字段补齐中文短标题，润色搬运、整批提炼与工具总结提示模板。

### 修复
- **启动竞态消除**：后台循环启动前增加 `3` 秒缓冲，避免与尚未加载完的 `mai-day` 产生启动期调用失败日志。

## [0.1.0] - 2026-11-26

### 新增
- 初始开发版本：当日 RSS 采集、观察群定向搬运、与 `mai-day` 四阶段租约协同、独立工具态 `/总结` 命令。

# 麦麦见闻

`zeyr.mai-insights` · 后台采集、整理见闻与定向分享。

开发基线 MaiBot 1.4.8 / SDK 2.23.0。可独立安装，其他 zeyr 插件仅通过公开 API 自动联动。

第三批已实现：RSS/RSSHub 当天条目采集、发布时间校验、URL/标题去重、摘要清洗、后台模型整理、观察群采集、source → targets 定向分享、独立冷却、`get_today_insights` / `mark_shared` API，以及 `query_today_insights` 工具。安装 `mai-day` 时通过租约、精确文本授权和实际回执协调分享窗口；没有 `mai-day` 时使用本地时间窗和冷却独立运行。`/总结` 与定时总结使用独立工具态，不同步到日常人设历史。正式名称为“麦麦见闻”，ID 为 `zeyr.mai-insights`。

默认来源和路线为空；观察群只采集，不会向来源群发言。后台发送只在实际 `send.text` 成功后写入分享回执。

安装时将整个目录同步到宿主 plugins；运行 config.toml 由 SDK 生成。实例名单、个人资料和密钥只填运行配置，不提交。

检查：`python -m pytest`，开发依赖见 pyproject.toml。

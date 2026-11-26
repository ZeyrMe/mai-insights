# 麦麦见闻

`zeyr.mai-insights` · 后台采集、整理见闻与定向分享。

开发基线 MaiBot 1.4.8 / SDK 2.23.0。可独立安装，其他 zeyr 插件仅通过公开 API 自动联动。

当前为第三批骨架：已完成配置、清晰的 API 预留和契约测试，但尚未开放当天采集、整理或定向分享运行时工具。正式名称为“麦麦见闻”，ID 为 `zeyr.mai-insights`；不会伪装成已实现功能。

安装时将整个目录同步到宿主 plugins；运行 config.toml 由 SDK 生成。实例名单、个人资料和密钥只填运行配置，不提交。

检查：`python -m pytest`，开发依赖见 pyproject.toml。

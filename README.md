# 麦麦见闻

`zeyr.mai-insights` · 后台采集、整理见闻与定向分享。

开发基线 MaiBot 1.4.8 / SDK 2.23.0。可独立安装，其他 zeyr 插件仅通过公开 API 自动联动。

第三批已实现：RSS/RSSHub 当天条目采集、发布时间校验、URL/标题去重、摘要清洗、后台模型整理、观察群采集、source → targets 定向分享、独立冷却、`get_today_insights` / `mark_shared` API，以及 `query_today_insights` 工具。安装 `mai-day` 时通过租约、精确文本授权和实际回执协调分享窗口；没有 `mai-day` 时使用本地时间窗和冷却独立运行。`/总结` 与定时总结使用独立工具态，不同步到日常人设历史；`/总结 群内` 通过宿主消息能力读取当天逻辑日窗口，也支持在其他会话指定来源群。正式名称为“麦麦见闻”，ID 为 `zeyr.mai-insights`。

默认来源和路线为空；观察群只采集，不会向来源群发言。后台发送只在实际 `send.text` 成功后写入分享回执。

安装时将整个目录同步到宿主 plugins；运行 config.toml 由 SDK 生成。实例名单、个人资料和密钥只填运行配置，不提交。

检查：`python -m pytest`，开发依赖见 pyproject.toml。


## v2.5 第一批：范围与原生控制台

新增独立 `[scope]`：mode 默认 all；groups / privates / users 接受 `group:标识` / `private:标识` / `user:标识`，可带平台前缀（默认 qq）。mode=whitelist 只允许命中对象；mode=blacklist 排除命中对象；all 允许所有对象。exclude 始终优先。allow_private=false 仅放行名单明确指定的私聊，主动私聊仍需单独配置目标。

可用 `@set:名称` / `@tag:名称` 复用 mai-circle 集合/标签；缺席时按空集合降级，不影响直接对象。受限模式在没有真实会话身份时跳过业务，消息仍正常进入宿主。重启后的会话身份缓存由入站消息恢复。

WebUI 配置页提供运行状态面板与“刷新公共集合缓存”按钮，不调用模型、不发送聊天。范围分组默认折叠；字段采用中文标签与结构化条目卡片。

现有 feeds / routes 行为保留；新版同时提供 N+M 订阅与批量消化。

本批提供公共基础设施。后续业务能力见下方 0.2.0 说明。旧配置保持可读，Runner 规范化写回后采用新结构；回退旧代码前需恢复相应配置快照。实例 config.toml 仍不进入 Git。


## 0.2.0 · v2.5 业务增量

feeds/observe_groups 贴 tags，targets 按 subscribe/exclude_sources/mode 路由，支持显式私聊。保留旧 routes 兼容。采集阶段不调用模型，观察群入站缓冲不逐条 fsync；整批 prepare 按条数/字数分段并分层归并，失败保留原料游标，中间摘要超限报错而非截断静默丢失。仅真实原料 ID 可成为当天精选，跨逻辑日完成的旧批次不进入新池。工具总结与活人分享分开；默认无来源/目标，独立运行也要求目标近期有人交流。观察群无条件只采不发。控制台支持手动采集、消化、清冷却。

接口保持 `@API(..., version="1")`，缺少可选插件时自动降级。模型生成、网络和宿主上下文效果仍需结合实例配置验收；离线测试不等同于真实聊天送达。

整批消化可通过 better-search 补充正文，默认每批最多3条、每条8000字；失败仍使用原摘要，采集阶段保持零模型调用。用 max_enrich_per_digest / enrich_body_chars 调整预算。

## 开发与门禁

此目录由 MaiBot 直接加载，不使用 `pip install .`。单独拉取本仓即可安装固定的测试环境，不依赖父目录或其他仓库：

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r .github/requirements-ci.txt
.venv/bin/python scripts/check.py
```

`check.py` 验证 Git 已跟踪文本的 UTF-8、行尾、末尾换行和尾随空格，然后运行 Ruff lint、Ruff 格式检查及全部 pytest。它不自动修复源码或重新生成示例配置；新增文件需先纳入 Git，才会参与文本检查。测试还验证示例配置与 SDK 默认值、manifest 身份/版本/运行依赖与 `pyproject.toml` 的一致性，以及已安装依赖是否满足声明。

GitHub Actions 在 push、pull request、手动触发和每周一运行：Python 3.12/3.13 验证、运行及开发依赖漏洞审计、Gitleaks 完整 Git 历史扫描。每仓独立执行，不调用共享 `tools`，不需要自定义密钥。Gitleaks 使用内置规则，固定 CLI 版本并校验下载 SHA-256；扫描日志隐藏发现的密钥值，不默认忽略历史发现。

本地安全检查（Gitleaks CLI 版本与 CI 保持一致）：

```bash
.venv/bin/python -m pip_audit --strict --require-hashes --disable-pip -r .github/requirements-ci.txt
gitleaks git --redact --log-opts="--all" .
gitleaks dir --redact .
```

`.gitattributes` 统一文本 LF，Windows 批处理为 CRLF，并明确二进制资源；`.editorconfig` 统一编辑器编码、缩进和空白。Ruff 负责 Python 代码格式；JSON/TOML 由契约测试解析校验。实例配置和秘密不得提交。

运行依赖范围与开发依赖在 `pyproject.toml` 中声明，`.github/requirements-ci.txt` 锁定 CI 的直接及间接依赖。修改依赖后，在仓库根目录使用 uv 重新解析并提交锁定文件；普通代码修改无需刷新依赖：

```bash
uv pip compile pyproject.toml --extra dev --universal --python-version 3.12 --generate-hashes --output-file .github/requirements-ci.txt
# 有意升级到兼容范围内的新版本时，在上述命令后加 --upgrade。
```

Dependabot 每周检查 Python 依赖和 GitHub Actions 更新。接入 GitHub 后，在分支保护或 ruleset 中将 `Python 3.12`、`Python 3.13`、`Dependency audit`、`Gitleaks` 设为合并必需检查；只提交工作流文件不会自动开启分支保护。

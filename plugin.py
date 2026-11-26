from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import json
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import feedparser
import httpx
from maibot_sdk import API, Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.config_widgets import ConfigWidget, ConfigWidgetState
from maibot_sdk.types import HookOrder

from .config_models import Config
from .ecosystem import EcosystemMixin, anchor
from .runtime import StateFile, identity, render, result, send_receipt, send_succeeded, service_send


def clean(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))).strip()


def canonical_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    query = [(k, v) for k, v in parse_qsl(parsed.query) if not k.lower().startswith("utm_")]
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path or "/", urlencode(query), ""))


def initial_state():
    return {"schema_version": 1, "items": [], "shared": {}, "cooldowns": {}, "outbox": {}, "summaries": {}}


class Plugin(EcosystemMixin, MaiBotPlugin):
    config_model = Config
    config_widgets = EcosystemMixin.config_widgets + [
        ConfigWidget(id="collect_now", kind="action", label="立即采集全部来源"),
        ConfigWidget(id="digest_now", kind="action", label="立即整批消化"),
        ConfigWidget(
            id="clear_cooldowns",
            kind="action",
            label="清空今日分享冷却",
            confirmation="清空冷却后可能更快主动分享，继续？",
        ),
    ]

    def __init__(self):
        super().__init__()
        self.store = None
        self.zone = ZoneInfo("Asia/Shanghai")
        self.task = None
        self.lock = asyncio.Lock()
        self.digest_lock = asyncio.Lock()
        self.jobs = {}
        self.health = {}

    def console_text(self):
        rows = self.today_items() if self.store else []
        return (
            f"来源健康：{self.health}\n观察群：{len(self.config.observe_groups)}；订阅目标：{len(self.config.targets)}\n未消化：{sum(not row.get('digested') for row in rows)}；今日精选：{sum(bool(row.get('prepared')) for row in rows)}\n精选："
            + "；".join(row["title"] for row in rows if row.get("prepared"))
        )

    async def on_load(self):
        self.zone = ZoneInfo(self.config.timezone)
        self.store = StateFile(self.ctx.paths.data_dir / "insights.json", initial_state())
        for key, default in initial_state().items():
            self.store.data.setdefault(key, default)
        self.task = asyncio.create_task(self.loop(), name="zeyr-mai-insights")

    async def on_unload(self):
        if self.store:
            self.store.save()
        for task in list(self.jobs.values()):
            task.cancel()
        if self.jobs:
            await asyncio.gather(*list(self.jobs.values()), return_exceptions=True)
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def on_config_update(self, scope, config_data, version):
        self.zone = ZoneInfo(self.config.timezone)

    async def loop(self):
        await asyncio.sleep(3)
        while True:
            try:
                if self.config.enabled:
                    self.store.save()
                    await self.collect()
                    await self.prepare()
                    await self.flush_outbox()
                    await self.share_one()
                    await self.scheduled_summaries()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ctx.logger.exception("见闻后台任务失败；保留状态，下轮重试")
            await asyncio.sleep(self.config.tick_seconds)

    def logical_now(self):
        return datetime.now(self.zone)

    def logical_date(self, now=None):
        return (
            (now or self.logical_now()).astimezone(self.zone) - timedelta(hours=self.config.day_rollover_hour)
        ).date()

    def today_items(self):
        day = self.logical_date().isoformat()
        return [row for row in self.store.data["items"] if row.get("logical_date") == day]

    def observation(self, platform, group):
        if any(anchor(row.group) == (platform, "group", group) for row in self.config.observe_groups):
            return True
        return f"{platform}:{group}" in self.config.passive_observation_groups or (
            platform == "qq" and group in self.config.passive_observation_groups
        )

    async def call_optional(self, name, **kwargs):
        try:
            async with asyncio.timeout(
                self.config.network_timeout_seconds if name.startswith("zeyr.better-search.") else 0.4
            ):
                if not await self.ctx.api.get(name, version="1"):
                    return None
                return await self.call(name, **kwargs)
        except Exception:
            return None

    async def call(self, name, **kwargs):
        async with asyncio.timeout(self.config.network_timeout_seconds):
            payload = await self.ctx.api.call(name, version="1", **kwargs)
        while isinstance(payload, dict) and "result" in payload and "schema_version" not in payload:
            if not payload.get("success"):
                raise ValueError("宿主 API 调用失败")
            payload = payload["result"]
        if not isinstance(payload, dict) or not payload.get("success"):
            raise ValueError("插件 API 调用失败")
        return payload.get("data")

    async def download(self, url):
        if not canonical_url(url):
            raise ValueError("RSS 地址需要 http/https")
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=self.config.network_timeout_seconds
        ) as client:
            async with asyncio.timeout(self.config.network_timeout_seconds):
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > self.config.max_response_bytes:
                            raise ValueError("RSS 超过下载上限")
                    return bytes(data)

    def add_item(self, *, title, summary, url, published_at, source, persist=True):
        now = self.logical_now()
        if (
            published_at.tzinfo is None
            or published_at > now
            or self.logical_date(published_at) != self.logical_date(now)
        ):
            return None
        title, summary, url = clean(title), clean(summary), canonical_url(url)
        if not title:
            return None
        normalized_title = re.sub(r"\W+", "", title).casefold()
        for row in self.today_items():
            if (url and row["url"] == url) or row.get("title_key") == normalized_title:
                row["tags"] = list(dict.fromkeys(row.get("tags", []) + self.source_tags(source)))
                return row
        key = hashlib.sha256(f"{source}|{url}|{normalized_title}|{self.logical_date()}".encode()).hexdigest()[
            :24
        ]
        row = {
            "id": key,
            "logical_date": self.logical_date().isoformat(),
            "source": source,
            "published_at": published_at.isoformat(),
            "title": title[:500],
            "title_key": normalized_title,
            "summary": summary,
            "url": url,
            "insight": "",
            "prepared": False,
            "tags": self.source_tags(source),
        }
        self.store.data["items"] = self.today_items() + [row]
        if persist:
            self.store.save()
        return row

    async def collect(self):
        if not self.config.enabled:
            return []
        for feed in self.config.feeds:
            if not feed.enabled or not (feed.url or feed.rsshub_route):
                continue
            try:
                urls = (
                    [feed.url]
                    if feed.url
                    else [
                        base.rstrip("/") + "/" + feed.rsshub_route.lstrip("/")
                        for base in self.config.rsshub_base_urls
                    ]
                )
                parsed = None
                for url in urls:
                    try:
                        parsed = feedparser.parse(await self.download(url))
                        break
                    except Exception:
                        continue
                if parsed is None:
                    raise ValueError("所有 RSSHub 地址均失败")
                self.health[feed.id or feed.url] = "ok"
                for entry in parsed.entries:
                    raw_date = entry.get("published") or entry.get("updated")
                    published = self.parse_date(raw_date)
                    if not published or not canonical_url(entry.get("link", "")):
                        continue
                    self.add_item(
                        title=entry.get("title", ""),
                        summary=entry.get("summary", ""),
                        url=entry.get("link", ""),
                        published_at=published,
                        source=feed.id or feed.url,
                    )
            except Exception as exc:
                self.health[feed.id or feed.url] = type(exc).__name__
                self.ctx.logger.warning("RSS %s 采集失败：%s", feed.id, type(exc).__name__)
        return self.today_items()

    def parse_date(self, value):
        if not value:
            return None
        from email.utils import parsedate_to_datetime

        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return None
        return parsed.astimezone(self.zone) if parsed.tzinfo else None

    async def generate(self, prompt):
        async with asyncio.timeout(self.config.generation_timeout_seconds):
            response = await self.ctx.llm.generate(prompt, model=self.config.model_task)
        if not isinstance(response, dict) or not response.get("success"):
            raise ValueError("后台模型未返回结果")
        return str(response.get("response") or response.get("text") or "").strip()

    def source_tags(self, source):
        if source.startswith("group:"):
            _, platform, group = source.split(":", 2)
            return next(
                (
                    row.tags
                    for row in self.config.observe_groups
                    if anchor(row.group) == (platform, "group", group)
                ),
                ["default"],
            )
        return next((feed.tags for feed in self.config.feeds if source in (feed.id, feed.url)), ["default"])

    def split_material(self, rows):
        chunks, current, size = [], [], 0
        for row in rows:
            # Long single inputs are split too; the raw record remains available for provenance.
            material = json.dumps(
                {
                    "item_id": row["id"],
                    "title": row["title"],
                    "source": row["source"],
                    "url": row["url"],
                    "summary": row["summary"],
                    "body": row.get("body", ""),
                },
                ensure_ascii=False,
            )
            for offset in range(0, len(material), self.config.batch_max_chars):
                part = material[offset : offset + self.config.batch_max_chars]
                if current and (
                    len(current) >= self.config.batch_max_items
                    or size + len(part) > self.config.batch_max_chars
                ):
                    chunks.append(current)
                    current, size = [], 0
                current.append(part)
                size += len(part)
        if current:
            chunks.append(current)
        return chunks

    async def digest_chunk(self, material):
        text = await self.generate(render(self.config.digest_instruction, material=material))
        if len(text) > self.config.batch_max_chars // 2:
            raise ValueError("中间摘要过长，保留全部原料和处理游标")
        return text

    async def prepare(self, force=False):
        if self.digest_lock.locked():
            return
        async with self.digest_lock:
            now = self.logical_now()
            stamp = self.store.data.get("digest_at")
            if (
                not force
                and stamp
                and now
                < datetime.fromisoformat(stamp) + timedelta(minutes=self.config.digest_interval_minutes)
            ):
                return
            state = await self.call_optional("zeyr.mai-day.get_state")
            if state and state.get("sleeping") and not force:
                return
            rows = [item for item in self.today_items() if not item.get("digested")]
            if not rows:
                return
            if self.config.enrich_with_search:
                candidates = [row for row in rows if row["url"] and "body" not in row][
                    : self.config.max_enrich_per_digest
                ]
                for row in candidates:
                    extra = await self.call_optional("zeyr.better-search.fetch", url=row["url"])
                    if extra and extra.get("text"):
                        row["body"] = str(extra["text"])[: self.config.enrich_body_chars]
            maps = []
            for chunk in self.split_material(rows):
                maps.append(await self.digest_chunk("\n".join(chunk)))
            # Hierarchical reduction keeps every map covered without a single unbounded prompt.
            while len(maps) > 1:
                groups, group, size = [], [], 0
                for value in maps:
                    if len(value) > self.config.batch_max_chars // 2:
                        raise ValueError("中间摘要超过安全合并容量，保留游标等待重试")
                    if group and size + len(value) > self.config.batch_max_chars:
                        groups.append(group)
                        group, size = [], 0
                    group.append(value)
                    size += len(value)
                if group:
                    groups.append(group)
                if len(groups) >= len(maps):
                    raise ValueError("摘要分段无法收敛，请调整 batch_max_chars")
                maps = [await self.digest_chunk("\n".join(group)) for group in groups]
            raw = maps[0].strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
            output = json.loads(raw)
            if (
                not isinstance(output, dict)
                or not isinstance(output.get("overview"), str)
                or not isinstance(output.get("insights", []), list)
            ):
                raise ValueError("批次输出必须是 overview / insights 结构")
            # Only existing items can be selected; no model-created IDs or links are accepted.
            if any(row["logical_date"] != self.logical_date().isoformat() for row in rows):
                return
            indexed = {row["id"]: row for row in rows}
            for selected in output.get("insights", [])[: self.config.max_insights_per_digest]:
                if (
                    isinstance(selected, dict)
                    and selected.get("item_id") in indexed
                    and isinstance(selected.get("text"), str)
                    and selected["text"].strip()
                ):
                    indexed[selected["item_id"]].update(insight=selected["text"][:1000], prepared=True)
            for row in rows:
                row["digested"] = True
            self.store.data.setdefault("digests", []).append(
                {
                    "logical_date": self.logical_date().isoformat(),
                    "overview": output["overview"],
                    "item_ids": [row["id"] for row in rows],
                    "at": now.isoformat(),
                }
            )
            self.store.data["digest_at"] = now.isoformat()
            self.store.save()

    async def get_config_widget_state(self):
        states = await super().get_config_widget_state()
        return {
            **states,
            **{
                name: ConfigWidgetState(disabled=name in self.jobs)
                for name in ("collect_now", "digest_now", "clear_cooldowns")
            },
        }

    async def console_job(self, action_id):
        try:
            if action_id == "collect_now":
                await self.collect()
            elif action_id == "digest_now":
                await self.prepare(force=True)
        except Exception:
            self.ctx.logger.exception("见闻控制台任务失败")
        finally:
            self.jobs.pop(action_id, None)

    async def on_config_widget_action(self, action_id):
        if action_id == "clear_cooldowns":
            self.store.data["cooldowns"] = {}
            self.store.save()
            return
        if action_id in ("collect_now", "digest_now"):
            if action_id not in self.jobs:
                self.jobs[action_id] = asyncio.create_task(self.console_job(action_id))
            return
        await super().on_config_widget_action(action_id)

    @HookHandler("chat.receive.after_process")
    async def observe_message(self, message=None, **kwargs):
        if not self.config.enabled:
            return {"action": "continue"}
        message = message or {}
        if not await self.in_scope(message, **kwargs):
            return {"action": "continue"}
        platform, user, group, _ = identity(message)
        bot = await self.call_optional("zeyr.mai-circle.is_bot", platform=platform, user_id=user)
        if bot and bot.get("value"):
            return {"action": "continue"}
        target = f"{platform}:group:{group}" if group else f"{platform}:private:{user}"
        self.store.data.setdefault("activity", {})[target] = self.logical_now().isoformat()
        if not group or not self.observation(platform, group):
            return {"action": "continue"}
        text = clean(message.get("processed_plain_text") or "")
        if text:
            stamp = message.get("time") or message.get("timestamp") or self.logical_now().isoformat()
            try:
                published = (
                    datetime.fromtimestamp(float(stamp), self.zone)
                    if isinstance(stamp, (int, float))
                    else self.parse_date(str(stamp))
                )
            except (ValueError, OverflowError):
                published = None
            if published:
                self.add_item(
                    title=text[:150],
                    summary=text,
                    url="",
                    published_at=published,
                    source=f"group:{platform}:{group}",
                    persist=False,
                )
        return {"action": "continue"}

    def local_share_allowed(self, target):
        stamp = self.store.data.setdefault("cooldowns", {}).get(target)
        # Explicit targets are mandatory; compatibility callers with opaque names use legacy local tests only.
        try:
            platform, kind, value = anchor(target)
        except ValueError:
            if target.count(":") == 1:
                platform, value = target.split(":", 1)
                kind = "group"
            else:
                platform, kind, value = "qq", "group", target
        key = f"{platform}:{kind}:{value}"
        active = self.store.data.get("activity", {}).get(key)
        if ":" in target and (
            not active
            or self.logical_now() - datetime.fromisoformat(active)
            > timedelta(minutes=self.config.active_recent_minutes)
        ):
            return False
        hour = self.logical_now().hour
        start, end = self.config.fallback_open_start_hour, self.config.fallback_open_end_hour
        in_window = start <= hour < end if start < end else hour >= start or hour < end
        return in_window and (
            not stamp
            or self.logical_now()
            >= datetime.fromisoformat(stamp) + timedelta(minutes=self.config.share_cooldown_minutes)
        )

    async def resolve_stream(self, platform, *, group_id="", user_id=""):
        response = await (
            self.ctx.chat.get_stream_by_group_id(group_id, platform=platform)
            if group_id
            else self.ctx.chat.get_stream_by_user_id(user_id, platform=platform)
        )
        if not isinstance(response, dict) or not response.get("success"):
            return ""
        stream = response.get("stream") or response
        return str(stream.get("stream_id") or stream.get("session_id") or "")

    def matches(self, item, route):
        return (
            item["source"] == f"group:{route.platform}:{route.source_group_id}"
            if item["source"].startswith("group:")
            else item["source"] in route.source_ids or "*" in route.source_ids
        )

    async def share_one(self):
        if not self.config.enabled or not self.config.share_enabled:
            return False
        async with self.lock:
            for target in self.config.targets:
                if target.mode == "digest":
                    continue
                platform, kind, value = anchor(target.chat)
                if kind == "group" and self.observation(platform, value):
                    continue
                if not await self.target_in_scope(
                    platform, value if kind == "group" else "", value if kind == "private" else ""
                ):
                    continue
                candidates = [
                    row
                    for row in self.today_items()
                    if row.get("prepared")
                    and row["source"] not in target.exclude_sources
                    and ("*" in target.subscribe or set(target.subscribe) & set(row.get("tags", ["default"])))
                ]
                stream = await self.resolve_stream(
                    platform,
                    group_id=value if kind == "group" else "",
                    user_id=value if kind == "private" else "",
                )
                if not stream:
                    continue
                for row in candidates:
                    if f"{stream}:{row['id']}" not in self.store.data["shared"] and self.local_share_allowed(
                        target.chat
                    ):
                        if await self.send_item(row, stream, target.chat):
                            return True
            for route in self.config.routes:
                for row in [
                    item for item in self.today_items() if item.get("prepared") and self.matches(item, route)
                ]:
                    for group in route.target_group_ids:
                        target = f"{route.platform}:group:{group}"
                        if not await self.target_in_scope(route.platform, group_id=group):
                            continue
                        if self.observation(route.platform, group) or group == route.source_group_id:
                            continue
                        if f"{target}:{row['id']}" in self.store.data["shared"]:
                            continue
                        if self.store.data.setdefault("cooldowns", {}).get(target):
                            stamp = datetime.fromisoformat(self.store.data["cooldowns"][target])
                            if self.logical_now() < stamp + timedelta(
                                minutes=self.config.share_cooldown_minutes
                            ):
                                continue
                        stream = await self.resolve_stream(route.platform, group_id=group)
                        if stream and await self.send_item(row, stream, target):
                            return True
        return False

    async def send_item(self, item, stream, target):
        lease, sent, receipt = None, False, ""
        try:
            async with asyncio.timeout(0.4):
                installed = await self.ctx.api.get("zeyr.mai-day.reserve_share", version="1")
            if installed:
                reservation = await self.call("zeyr.mai-day.reserve_share", stream_id=stream)
                if not reservation or not reservation.get("value"):
                    return False
                lease = reservation["lease"]
            elif not self.local_share_allowed(target):
                return False
            async with asyncio.timeout(self.config.generation_timeout_seconds):
                generated = await self.ctx.maisaka.reply.generate(
                    stream, reference=item["insight"], format_instruction=self.config.share_instruction
                )
            if not isinstance(generated, dict) or not generated.get("success") or not generated.get("text"):
                return False
            if item.get("logical_date", self.logical_date().isoformat()) != self.logical_date().isoformat():
                return False
            text = generated["text"].strip()
            if lease:
                authorization = await self.call(
                    "zeyr.mai-day.authorize_share", lease_id=lease["id"], text=text
                )
                if not authorization or not authorization.get("value"):
                    return False
            response = await self.ctx.send.text(
                text, stream, return_details=True, sync_to_maisaka_history=True
            )
            sent = send_succeeded(response)
            receipt = send_receipt(response, (lease or {}).get("id") or item["id"]) if sent else ""
            if sent:
                # Persist the actual send before remote settlement so retries never duplicate a delivered message.
                self.mark_delivery(item["id"], stream, receipt, target)
                if lease:
                    self.store.data.setdefault("outbox", {})[lease["id"]] = {
                        "lease_id": lease["id"],
                        "sent": True,
                        "receipt_id": receipt,
                        "text": text,
                        "item_id": item["id"],
                    }
                    self.store.save()
            return sent
        except Exception:
            return False
        finally:
            if lease and not sent:
                with contextlib.suppress(Exception):
                    await self.call("zeyr.mai-day.settle_share", lease_id=lease["id"], sent=False)
            if sent:
                await self.flush_outbox()

    def mark_delivery(self, item_id, stream, receipt, target=""):
        self.store.data["shared"][f"{stream}:{item_id}"] = {
            "receipt_id": receipt,
            "at": self.logical_now().isoformat(),
        }
        if target:
            self.store.data["shared"][f"{target}:{item_id}"] = receipt
            self.store.data.setdefault("cooldowns", {})[target] = self.logical_now().isoformat()
        self.store.save()

    async def flush_outbox(self):
        for key, payload in list(self.store.data.setdefault("outbox", {}).items()):
            try:
                await self.call(
                    "zeyr.mai-day.settle_share", lease_id=key, sent=True, receipt_id=payload["receipt_id"]
                )
                await self.call(
                    "zeyr.mai-day.append_dynamic_event",
                    text=payload["text"],
                    source="mai-insights",
                    event_id=f"insight:{key}",
                )
            except Exception:
                continue
            del self.store.data["outbox"][key]
            self.store.save()

    @Tool(
        "query_today_insights",
        description="立即查询后台已整理的今日见闻，不现场抓取。",
        parameters={"topic": {"type": "string", "default": ""}},
        visibility="visible",
    )
    async def query_insights(self, topic="", **kwargs):
        if not await self.in_scope(**kwargs):
            return result(error="当前会话不在插件生效范围")
        rows = [row for row in self.today_items() if row.get("prepared")] if self.config.enabled else []
        if topic:
            rows = [row for row in rows if topic.casefold() in (row["title"] + row["summary"]).casefold()]
        return result({"logical_date": self.logical_date().isoformat(), "items": rows})

    @API("get_today_insights", public=True, version="1")
    async def api_today(self, limit=3, topic="", **kwargs):
        try:
            rows = (
                [
                    row
                    for row in self.today_items()
                    if row.get("prepared")
                    and (not topic or topic.casefold() in (row["title"] + row["insight"]).casefold())
                ][: max(1, min(int(limit), self.config.max_items))]
                if self.config.enabled
                else []
            )
            return result({"logical_date": self.logical_date().isoformat(), "items": rows})
        except (ValueError, TypeError):
            return result(error="limit 必须是整数")

    @API("mark_shared", public=True, version="1")
    async def api_mark_shared(self, item_id, stream_id, receipt_id, **kwargs):
        if (
            not item_id
            or not stream_id
            or not receipt_id
            or not any(row["id"] == item_id for row in self.today_items())
        ):
            return result(error="需要当天存在的 item_id、stream_id 和成功 receipt_id")
        self.mark_delivery(item_id, stream_id, receipt_id)
        return result({"marked": True, "key": f"{stream_id}:{item_id}"})

    async def summary(self, source="all"):
        if source.startswith("group:"):
            _, platform, group_id = source.split(":", 2)
            stream = await self.resolve_stream(platform, group_id=group_id)
            if not stream:
                raise ValueError("找不到来源群会话")
            return await self.group_summary(stream)
        digests = [
            row
            for row in self.store.data.get("digests", [])
            if row["logical_date"] == self.logical_date().isoformat()
        ]
        if source == "all" and digests:
            return "\n".join(row["overview"] for row in digests)[-self.config.summary_max_chars :]
        rows = self.today_items()
        if source == "feeds":
            rows = [row for row in rows if not row["source"].startswith("group:")]
        elif source != "all":
            rows = [row for row in rows if row["source"] == source]
        if not rows:
            return "当前逻辑日没有符合条件的资料。"
        material = json.dumps(
            [{key: row[key] for key in ("title", "summary", "source", "url")} for row in rows],
            ensure_ascii=False,
        )
        text = await self.generate(render(self.config.summary_instruction, material=material[:30000]))
        return text[: self.config.summary_max_chars]

    async def group_summary(self, stream):
        now = self.logical_now()
        start = datetime.combine(self.logical_date(now), datetime.min.time(), self.zone) + timedelta(
            hours=self.config.day_rollover_hour
        )
        # Host 1.4.8 converts these values with float(), despite SDK's str annotations.
        async with asyncio.timeout(self.config.network_timeout_seconds):
            response = await self.ctx.message.get_by_time_in_chat(
                stream,
                str(start.timestamp()),
                str(now.timestamp()),
                limit=self.config.summary_message_limit,
                limit_mode="latest",
                filter_command=True,
                include_binary_data=False,
            )
        if not isinstance(response, dict) or not response.get("success"):
            raise ValueError("读取群消息失败")
        rows = response.get("messages") or []
        material = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            text = clean(row.get("processed_plain_text") or "")
            if not text or text.startswith("/总结"):
                continue
            _, user, _, _ = identity(row)
            material.append(
                {"user": user, "text": text[:2000], "time": row.get("timestamp") or row.get("time")}
            )
        if not material:
            return "当前逻辑日没有符合条件的资料。"
        text = await self.generate(
            render(
                self.config.summary_instruction, material=json.dumps(material, ensure_ascii=False)[-30000:]
            )
        )
        return text[: self.config.summary_max_chars]

    @Command("summarize", pattern=r"^/总结(?:\s+(?P<scope>群内|全网|全部)(?:\s+(?P<group_id>\S+))?)?$")
    async def summarize_command(self, message=None, matched_groups=None, stream_id="", **kwargs):
        if not await self.in_scope(message, stream_id=stream_id):
            return False, "当前会话不在插件生效范围", 1
        if not self.config.enabled:
            return False, "插件已禁用", 1
        platform, user, group, _ = identity(message or {})
        admin = any(entry.platform == platform and entry.user_id == user for entry in self.config.admins)
        if not admin:
            external = await self.call_optional("zeyr.mai-circle.is_admin", platform=platform, user_id=user)
            admin = bool(external and external.get("value"))
        if not admin:
            return False, "需要管理员权限", 1
        if self.observation(platform, group):
            return False, "观察群不发送总结；请在其他群或私聊指定来源群", 1
        scope = (matched_groups or {}).get("scope") or "全部"
        source_group = (matched_groups or {}).get("group_id") or group
        if scope == "群内" and not source_group:
            return False, "私聊请使用 /总结 群内 群号", 1
        if (matched_groups or {}).get("group_id") and scope != "群内":
            return False, "仅群内总结支持指定来源群", 1
        source = (
            f"group:{platform}:{source_group}" if scope == "群内" else "feeds" if scope == "全网" else "all"
        )
        try:
            text = await self.summary(source)
            response = await service_send(
                self.ctx, text, stream_id, return_details=True, sync_to_maisaka_history=False
            )
            sent = send_succeeded(response)
            return sent, "总结完成" if sent else "总结未送达，可稍后重试", 2 if sent else 1
        except Exception:
            return False, "总结生成失败，可稍后重试", 1

    async def scheduled_summaries(self):
        now = self.logical_now()
        if now.hour not in self.config.summary_hours:
            return
        for target in self.config.targets:
            if target.mode == "insight":
                continue
            platform, kind, value = anchor(target.chat)
            if (kind == "group" and self.observation(platform, value)) or not await self.target_in_scope(
                platform, value if kind == "group" else "", value if kind == "private" else ""
            ):
                continue
            key = f"{now.date()}:{now.hour}:target:{target.chat}"
            if key in self.store.data.setdefault("summaries", {}):
                continue
            rows = [
                row
                for row in self.today_items()
                if row["source"] not in target.exclude_sources
                and ("*" in target.subscribe or set(target.subscribe) & set(row.get("tags", ["default"])))
            ]
            if not rows:
                continue
            parts = [
                await self.generate(render(self.config.summary_instruction, material="\n".join(chunk)))
                for chunk in self.split_material(rows)
            ]
            text = "\n".join(parts)[: self.config.summary_max_chars]
            stream = await self.resolve_stream(
                platform,
                group_id=value if kind == "group" else "",
                user_id=value if kind == "private" else "",
            )
            if stream:
                sent = await service_send(
                    self.ctx, text, stream, return_details=True, sync_to_maisaka_history=False
                )
                if send_succeeded(sent):
                    self.store.data["summaries"][key] = send_receipt(sent, key)
                    self.store.save()
        for target in self.config.summary_targets:
            if not await self.target_in_scope(target.platform, target.group_id, target.user_id):
                continue
            if target.group_id and self.observation(target.platform, target.group_id):
                continue
            key = f"{now.date()}:{now.hour}:{target.model_dump_json()}"
            if key in self.store.data.setdefault("summaries", {}):
                continue
            stream = await self.resolve_stream(
                target.platform, group_id=target.group_id, user_id=target.user_id
            )
            if not stream:
                continue
            text = await self.summary(target.source)
            response = await service_send(
                self.ctx, text, stream, return_details=True, sync_to_maisaka_history=False
            )
            if send_succeeded(response):
                self.store.data["summaries"][key] = send_receipt(response, key)
                self.store.save()

    @HookHandler("send_service.before_send", order=HookOrder.LATE)
    async def guard_send(self, message=None, **kwargs):
        platform, _, group, _ = identity(message or {})
        if self.config.enabled and group and self.observation(platform, group):
            return {"action": "abort", "custom_result": {"reason": "observation_only"}}
        return {"action": "continue"}


def create_plugin():
    return Plugin()

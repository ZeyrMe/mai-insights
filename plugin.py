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
from maibot_sdk.types import HookOrder

from .config_models import Config
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


class Plugin(MaiBotPlugin):
    config_model = Config

    def __init__(self):
        super().__init__()
        self.store = None
        self.zone = ZoneInfo("Asia/Shanghai")
        self.task = None
        self.lock = asyncio.Lock()

    async def on_load(self):
        self.zone = ZoneInfo(self.config.timezone)
        self.store = StateFile(self.ctx.paths.data_dir / "insights.json", initial_state())
        for key, default in initial_state().items():
            self.store.data.setdefault(key, default)
        self.task = asyncio.create_task(self.loop(), name="zeyr-mai-insights")

    async def on_unload(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def on_config_update(self, scope, config_data, version):
        self.zone = ZoneInfo(self.config.timezone)

    async def loop(self):
        while True:
            try:
                if self.config.enabled:
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
        return f"{platform}:{group}" in self.config.passive_observation_groups or (
            platform == "qq" and group in self.config.passive_observation_groups
        )

    async def call_optional(self, name, **kwargs):
        try:
            async with asyncio.timeout(self.config.network_timeout_seconds):
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

    def add_item(self, *, title, summary, url, published_at, source):
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
            "summary": summary[:4000],
            "url": url,
            "insight": "",
            "prepared": False,
        }
        self.store.data["items"] = (self.today_items() + [row])[-self.config.max_items :]
        self.store.save()
        return row

    async def collect(self):
        if not self.config.enabled:
            return []
        for feed in self.config.feeds:
            if not feed.enabled or not feed.url:
                continue
            try:
                parsed = feedparser.parse(await self.download(feed.url))
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

    async def prepare(self):
        # Generation runs only in this background stage; queries return the stored pool immediately.
        for row in [item for item in self.today_items() if not item.get("prepared")][
            : self.config.prepare_per_tick
        ]:
            try:
                material = {key: row[key] for key in ("title", "summary", "url", "published_at")}
                if self.config.enrich_with_search and row["url"]:
                    extra = await self.call_optional("zeyr.better-search.fetch", url=row["url"])
                    if extra:
                        material["body"] = str(extra.get("text") or "")[:8000]
                text = await self.generate(
                    render(self.config.insight_instruction, material=json.dumps(material, ensure_ascii=False))
                )
                if text:
                    row.update(insight=text[:1000], prepared=True)
                    self.store.save()
            except Exception:
                continue

    @HookHandler("chat.receive.after_process")
    async def observe_message(self, message=None, **kwargs):
        if not self.config.enabled:
            return {"action": "continue"}
        message = message or {}
        platform, user, group, _ = identity(message)
        if not group or not self.observation(platform, group):
            return {"action": "continue"}
        bot = await self.call_optional("zeyr.mai-circle.is_bot", platform=platform, user_id=user)
        if bot and bot.get("value"):
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
                )
        return {"action": "continue"}

    def local_share_allowed(self, target):
        stamp = self.store.data.setdefault("cooldowns", {}).get(target)
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
            for route in self.config.routes:
                for row in [
                    item for item in self.today_items() if item.get("prepared") and self.matches(item, route)
                ]:
                    for group in route.target_group_ids:
                        target = f"{route.platform}:{group}"
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
            async with asyncio.timeout(self.config.network_timeout_seconds):
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
        rows = self.today_items() if self.config.enabled else []
        if topic:
            rows = [row for row in rows if topic.casefold() in (row["title"] + row["summary"]).casefold()]
        return result({"logical_date": self.logical_date().isoformat(), "items": rows})

    @API("get_today_insights", public=True, version="1")
    async def api_today(self, limit=20, **kwargs):
        try:
            rows = (
                self.today_items()[: max(1, min(int(limit), self.config.max_items))]
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
        for target in self.config.summary_targets:
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

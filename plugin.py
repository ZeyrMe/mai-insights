from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import re
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from maibot_sdk import API, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import HookOrder

from .config_models import Config
from .runtime import StateFile, result


def clean(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


class Plugin(MaiBotPlugin):
    config_model = Config

    def __init__(self):
        super().__init__()
        self.store = None
        self.zone = ZoneInfo("Asia/Shanghai")
        self.task = None

    async def on_load(self):
        self.zone = ZoneInfo(self.config.timezone)
        self.store = StateFile(
            self.ctx.paths.data_dir / "insights.json", {"schema_version": 1, "items": [], "shared": {}}
        )
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
                await self.collect()
                await self.share_one()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ctx.logger.exception("见闻采集或定向分享失败；下轮重试")
            await asyncio.sleep(self.config.tick_seconds)

    def logical_now(self):
        return datetime.now(self.zone)

    def logical_date(self, now=None):
        return ((now or self.logical_now()) - timedelta(hours=self.config.day_rollover_hour)).date()

    def today_items(self):
        day = self.logical_date().isoformat()
        return [row for row in self.store.data["items"] if row.get("logical_date") == day]

    def local_share_allowed(self, target_group):
        stamp = self.store.data["shared"].get(f"cooldown:{target_group}")
        if not stamp:
            return True
        return self.logical_now() >= datetime.fromisoformat(stamp) + timedelta(
            minutes=self.config.share_cooldown_minutes
        )

    def parse_date(self, value):
        if not value:
            return None
        try:
            return parsedate_to_datetime(value).astimezone(self.zone)
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed.astimezone(self.zone) if parsed.tzinfo else parsed.replace(tzinfo=self.zone)
            except ValueError:
                return None

    async def collect(self):
        if not self.config.enabled:
            return []
        day = self.logical_date()
        collected = []
        for feed in self.config.feeds:
            if not feed.enabled or not feed.url:
                continue
            try:
                request = Request(feed.url, headers={"User-Agent": "zeyr-mai-insights/0.1"})
                body = await asyncio.to_thread(lambda: urlopen(request, timeout=10).read())
                root = ElementTree.fromstring(body)
                entries = root.findall(".//item") + root.findall(".//{http://www.w3.org/2005/Atom}entry")
                for entry in entries:
                    atom_link = entry.find("{http://www.w3.org/2005/Atom}link")
                    title = clean(
                        entry.findtext("title") or entry.findtext("{http://www.w3.org/2005/Atom}title")
                    )
                    link = entry.findtext("link") or (
                        atom_link.attrib.get("href", "") if atom_link is not None else ""
                    )
                    published = self.parse_date(
                        entry.findtext("pubDate") or entry.findtext("published") or entry.findtext("updated")
                    )
                    if not title or not link or not published or published.date() != day:
                        continue
                    summary = clean(entry.findtext("description") or entry.findtext("summary") or "")
                    item_id = hashlib.sha256(f"{link}|{title}".encode()).hexdigest()[:24]
                    collected.append(
                        {
                            "id": item_id,
                            "logical_date": day.isoformat(),
                            "source": feed.id or feed.url,
                            "published_at": published.isoformat(),
                            "title": title,
                            "summary": summary[:1000],
                            "url": link,
                            "insight": self.config.insight_template.format(
                                title=title, summary=summary[:240]
                            ),
                        }
                    )
            except Exception:
                continue
        merged = {row["id"]: row for row in self.store.data["items"]}
        merged.update({row["id"]: row for row in collected})
        self.store.data["items"] = list(merged.values())[-self.config.max_items :]
        self.store.save()
        return self.today_items()

    def observation_route(self, group_id):
        source = str(group_id)
        if source not in self.config.passive_observation_groups:
            return []
        return [route for route in self.config.routes if route.source_group_id == source]

    async def add_observation(self, message):
        if not self.config.enabled:
            return {"action": "continue"}
        info = message.get("message_info") or {}
        group = str((info.get("group_info") or {}).get("group_id") or message.get("group_id") or "")
        routes = self.observation_route(group)
        if not routes:
            return {"action": "continue"}
        text = clean(message.get("processed_plain_text") or "")
        if not text:
            return {"action": "continue"}
        day = self.logical_date().isoformat()
        item_id = hashlib.sha256(f"observation|{group}|{text}".encode()).hexdigest()[:24]
        rows = {row["id"]: row for row in self.store.data["items"]}
        rows.setdefault(
            item_id,
            {
                "id": item_id,
                "logical_date": day,
                "source": f"group:{group}",
                "published_at": self.logical_now().isoformat(),
                "title": "群内见闻",
                "summary": text[:1000],
                "url": "",
                "insight": text[:1000],
            },
        )
        self.store.data["items"] = list(rows.values())[-self.config.max_items :]
        self.store.save()
        return {"action": "continue"}

    async def share_one(self):
        if not self.config.enabled or not self.config.share_enabled:
            return False
        for route in self.config.routes:
            source_items = [
                row for row in self.today_items() if row["source"] == f"group:{route.source_group_id}"
            ]
            for item in source_items:
                for target_group in route.target_group_ids:
                    key = f"{target_group}:{item['id']}"
                    if key in self.store.data["shared"] or not self.local_share_allowed(target_group):
                        continue
                    stream = await self.ctx.chat.get_stream_by_group_id(target_group)
                    stream_id = stream.get("stream_id", "") if isinstance(stream, dict) else str(stream or "")
                    if not stream_id:
                        continue
                    lease = None
                    try:
                        api = await self.ctx.api.get("zeyr.mai-day.reserve_share", version="1")
                        if api:
                            response = await self.ctx.api.call(
                                "zeyr.mai-day.reserve_share", version="1", stream_id=stream_id
                            )
                            payload = response.get("result", response) if isinstance(response, dict) else {}
                            lease = (
                                (payload.get("data") or {}).get("lease") if payload.get("success") else None
                            )
                            if not lease:
                                continue
                        text = item["insight"]
                        if lease:
                            authorized = await self.ctx.api.call(
                                "zeyr.mai-day.authorize_share", version="1", lease_id=lease["id"], text=text
                            )
                            payload = (
                                authorized.get("result", authorized) if isinstance(authorized, dict) else {}
                            )
                            if not (payload.get("data") or {}).get("value"):
                                await self.ctx.api.call(
                                    "zeyr.mai-day.settle_share", version="1", lease_id=lease["id"], sent=False
                                )
                                continue
                        sent_result = await self.ctx.send.text(text, stream_id, return_details=True)
                        sent = bool(
                            isinstance(sent_result, dict)
                            and sent_result.get("success")
                            and sent_result.get("sent")
                        )
                        receipt = (
                            str(sent_result.get("message_id", "")) if isinstance(sent_result, dict) else ""
                        )
                        if lease:
                            await self.ctx.api.call(
                                "zeyr.mai-day.settle_share",
                                version="1",
                                lease_id=lease["id"],
                                sent=sent,
                                receipt_id=receipt,
                            )
                        if sent:
                            self.store.data["shared"][key] = self.logical_now().isoformat()
                            self.store.data["shared"][f"cooldown:{target_group}"] = (
                                self.logical_now().isoformat()
                            )
                            self.store.save()
                            return True
                    except Exception:
                        if lease:
                            with contextlib.suppress(Exception):
                                await self.ctx.api.call(
                                    "zeyr.mai-day.settle_share", version="1", lease_id=lease["id"], sent=False
                                )
                        continue
        return False

    @Tool(
        "query_today_insights",
        description="查询今天已整理的见闻；不会临时阻塞搜索。",
        parameters={"topic": {"type": "string", "default": ""}},
        visibility="visible",
    )
    async def query_insights(self, topic="", **kwargs):
        rows = await self.collect()
        if topic:
            rows = [row for row in rows if topic.casefold() in (row["title"] + row["summary"]).casefold()]
        return result({"logical_date": self.logical_date().isoformat(), "items": rows})

    @API("get_today_insights", public=True, version="1")
    async def api_today(self, limit=20, **kwargs):
        rows = (await self.collect())[: max(1, min(int(limit), self.config.max_items))]
        return result({"logical_date": self.logical_date().isoformat(), "items": rows})

    @API("mark_shared", public=True, version="1")
    async def api_mark_shared(self, item_id, stream_id, receipt_id, **kwargs):
        if not item_id or not stream_id or not receipt_id:
            return result(error="item_id、stream_id、receipt_id 均不能为空")
        key = f"{stream_id}:{item_id}:{receipt_id}"
        if key not in self.store.data["shared"]:
            self.store.data["shared"][key] = self.logical_now().isoformat()
            self.store.save()
        return result({"marked": True, "key": key})

    @HookHandler("maisaka.planner.before_request", order=HookOrder.NORMAL)
    async def planner(self, **kwargs):
        return {"action": "continue"}

    @HookHandler("chat.receive.after_process")
    async def observe_message(self, message=None, **kwargs):
        return await self.add_observation(message or {})


def create_plugin():
    return Plugin()

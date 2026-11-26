from datetime import datetime

import pytest
from conftest import module


@pytest.fixture
def plugin(tmp_path):
    instance = module.Plugin()
    instance.set_plugin_config({"plugin": {"config_version": "0.1.0"}})
    instance.store = module.StateFile(tmp_path / "insights.json", module.initial_state())
    instance.logical_now = lambda: datetime.fromisoformat("2026-11-27T12:00:00+08:00")
    return instance


@pytest.mark.asyncio
async def test_collects_only_current_logical_day_and_deduplicates(plugin, monkeypatch):
    xml = """<rss><channel>
      <item><title>今天新闻</title><link>https://x.test/today</link><pubDate>Fri, 27 Nov 2026 09:00:00 +0800</pubDate><description><![CDATA[<b>摘要</b>]]></description></item>
      <item><title>昨天新闻</title><link>https://x.test/yesterday</link><pubDate>Thu, 26 Nov 2026 09:00:00 +0800</pubDate></item>
    </channel></rss>""".encode()

    async def fake_download(url):
        return xml

    monkeypatch.setattr(plugin, "download", fake_download)
    from test_mai_insights.config_models import Feed

    plugin.config.feeds = [Feed(id="feed", url="https://feed.test/rss")]
    first = await plugin.collect()
    second = await plugin.collect()
    assert len(first) == len(second) == 1
    assert first[0]["title"] == "今天新闻"
    assert first[0]["summary"] == "摘要"


@pytest.mark.asyncio
async def test_mark_shared_is_idempotent(plugin):
    plugin.store.data["items"].append(
        {
            "id": "item",
            "logical_date": plugin.logical_date().isoformat(),
            "source": "feed",
            "published_at": plugin.logical_now().isoformat(),
            "title": "x",
            "title_key": "x",
            "summary": "x",
            "url": "https://x.test",
            "insight": "x",
            "prepared": True,
        }
    )
    first = await plugin.api_mark_shared("item", "group", "receipt")
    second = await plugin.api_mark_shared("item", "group", "receipt")
    assert first["data"] == second["data"]
    assert len(plugin.store.data["shared"]) == 1


def test_local_share_cooldown(plugin):
    assert plugin.local_share_allowed("group")
    plugin.store.data["cooldowns"]["group"] = plugin.logical_now().isoformat()
    assert not plugin.local_share_allowed("group")


@pytest.mark.asyncio
async def test_summary_is_bounded_and_does_not_require_chat_context(plugin):
    plugin.store.data["items"].append(
        {
            "id": "item",
            "logical_date": plugin.logical_date().isoformat(),
            "source": "feed",
            "published_at": plugin.logical_now().isoformat(),
            "title": "x",
            "title_key": "x",
            "summary": "摘要",
            "url": "https://x.test",
            "insight": "已整理见闻",
            "prepared": True,
        }
    )
    plugin.generate = lambda prompt: None
    summary = "\n".join(row["insight"] for row in plugin.today_items())
    assert "已整理见闻" in summary


def summary_context(plugin, *, messages=None):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    plugin.config.admins = [module.config_models.Admin(user_id="admin")]
    plugin._set_context(
        SimpleNamespace(
            chat=SimpleNamespace(
                get_stream_by_group_id=AsyncMock(
                    return_value={"success": True, "stream": {"stream_id": "source-stream"}}
                )
            ),
            message=SimpleNamespace(
                get_by_time_in_chat=AsyncMock(
                    return_value={
                        "success": True,
                        "messages": messages
                        or [
                            {
                                "processed_plain_text": "今天讨论了新版本",
                                "message_info": {"user_info": {"user_id": "friend"}},
                            }
                        ],
                    }
                )
            ),
            llm=SimpleNamespace(generate=AsyncMock(return_value={"success": True, "response": "工具总结"})),
            send=SimpleNamespace(text=AsyncMock(return_value={"sent": True, "message_id": "receipt"})),
        )
    )
    return {
        "platform": "qq",
        "message_info": {"user_info": {"user_id": "admin"}, "group_info": {"group_id": "ordinary"}},
    }


@pytest.mark.asyncio
async def test_group_summary_reads_host_messages_in_ordinary_group(plugin):
    message = summary_context(plugin)
    response = await plugin.summarize_command(
        message=message, matched_groups={"scope": "群内"}, stream_id="destination"
    )
    assert response[0]
    plugin.ctx.chat.get_stream_by_group_id.assert_awaited_once_with("ordinary", platform="qq")
    args = plugin.ctx.message.get_by_time_in_chat.await_args
    assert args.args[0] == "source-stream"
    assert datetime.fromtimestamp(float(args.args[1]), plugin.zone).hour == 5
    assert float(args.args[2]) == plugin.logical_now().timestamp()
    plugin.ctx.send.text.assert_awaited_once_with(
        "工具总结", "destination", return_details=True, sync_to_maisaka_history=False
    )
    assert "今天讨论了新版本" in plugin.ctx.llm.generate.await_args.args[0]


@pytest.mark.asyncio
async def test_private_summary_can_read_observation_group_but_never_send_there(plugin):
    message = summary_context(plugin)
    plugin.config.passive_observation_groups = ["qq:observe"]
    message["message_info"].pop("group_info")
    response = await plugin.summarize_command(
        message=message, matched_groups={"scope": "群内", "group_id": "observe"}, stream_id="private"
    )
    assert response[0]
    plugin.ctx.chat.get_stream_by_group_id.assert_awaited_with("observe", platform="qq")
    message["message_info"]["group_info"] = {"group_id": "observe"}
    response = await plugin.summarize_command(
        message=message, matched_groups={"scope": "群内"}, stream_id="observe-stream"
    )
    assert not response[0]
    assert plugin.ctx.send.text.await_count == 1
    assert plugin.ctx.message.get_by_time_in_chat.await_count == 1


@pytest.mark.asyncio
async def test_scheduled_group_summary_records_sdk_receipt_once(plugin):
    from test_mai_insights.config_models import SummaryTarget

    summary_context(plugin)
    plugin.config.summary_hours = [12]
    plugin.config.summary_targets = [SummaryTarget(user_id="admin", source="group:qq:observe")]
    from unittest.mock import AsyncMock

    plugin.resolve_stream = AsyncMock(side_effect=["private", "source-stream", "private"])
    await plugin.scheduled_summaries()
    await plugin.scheduled_summaries()
    assert list(plugin.store.data["summaries"].values()) == ["receipt"]
    plugin.ctx.send.text.assert_awaited_once()
    assert plugin.ctx.send.text.await_args.kwargs["sync_to_maisaka_history"] is False


@pytest.mark.asyncio
async def test_share_records_delivery_without_sdk_success_field(plugin):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    plugin._set_context(
        SimpleNamespace(
            api=SimpleNamespace(get=AsyncMock(return_value=False)),
            maisaka=SimpleNamespace(
                reply=SimpleNamespace(generate=AsyncMock(return_value={"success": True, "text": "新鲜见闻"}))
            ),
            send=SimpleNamespace(text=AsyncMock(return_value={"sent": True, "message_id": "receipt"})),
        )
    )
    assert await plugin.send_item({"id": "item", "insight": "材料"}, "s", "qq:target")
    assert plugin.store.data["shared"]["qq:target:item"] == "receipt"

from datetime import datetime

import pytest
from conftest import module


@pytest.fixture
def plugin(tmp_path):
    instance = module.Plugin()
    instance.set_plugin_config({"plugin": {"config_version": "0.1.0"}})
    instance.store = module.StateFile(
        tmp_path / "insights.json", {"schema_version": 1, "items": [], "shared": {}}
    )
    instance.logical_now = lambda: datetime.fromisoformat("2026-11-27T12:00:00+08:00")
    return instance


@pytest.mark.asyncio
async def test_collects_only_current_logical_day_and_deduplicates(plugin, monkeypatch):
    xml = """<rss><channel>
      <item><title>今天新闻</title><link>https://x.test/today</link><pubDate>Fri, 27 Nov 2026 09:00:00 +0800</pubDate><description><![CDATA[<b>摘要</b>]]></description></item>
      <item><title>昨天新闻</title><link>https://x.test/yesterday</link><pubDate>Thu, 26 Nov 2026 09:00:00 +0800</pubDate></item>
    </channel></rss>""".encode()

    class Response:
        def read(self):
            return xml

    monkeypatch.setattr(module, "urlopen", lambda *args, **kwargs: Response())
    plugin.config.feeds = [module.config_models.Feed(id="feed", url="https://feed.test/rss")]
    first = await plugin.collect()
    second = await plugin.collect()
    assert len(first) == len(second) == 1
    assert first[0]["title"] == "今天新闻"
    assert first[0]["summary"] == "摘要"


@pytest.mark.asyncio
async def test_mark_shared_is_idempotent(plugin):
    first = await plugin.api_mark_shared("item", "group", "receipt")
    second = await plugin.api_mark_shared("item", "group", "receipt")
    assert first["data"] == second["data"]
    assert len(plugin.store.data["shared"]) == 1


def test_local_share_cooldown(plugin):
    assert plugin.local_share_allowed("group")
    plugin.store.data["shared"]["cooldown:group"] = plugin.logical_now().isoformat()
    assert not plugin.local_share_allowed("group")

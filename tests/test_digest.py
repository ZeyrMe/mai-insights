import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from conftest import module


def make(tmp_path):
    p = module.Plugin()
    p.set_plugin_config({"plugin": {"config_version": "0.1.0"}, "batch_max_items": 1, "batch_max_chars": 500})
    p.store = module.StateFile(tmp_path / "insights.json", module.initial_state())
    p.logical_now = lambda: datetime.fromisoformat("2026-11-27T12:00:00+08:00")
    p._set_context(SimpleNamespace(api=SimpleNamespace(get=AsyncMock(return_value=None)), logger=Mock()))
    return p


async def test_collection_uses_zero_models_and_batch_commits_only_after_reduce(tmp_path):
    p = make(tmp_path)
    p.config.max_items = 1  # 精选容量不能截断原料
    for i in range(3):
        p.add_item(
            title=f"材料{i}",
            summary=f"摘要{i}",
            url=f"https://news.example/{i}",
            published_at=p.logical_now(),
            source="f",
        )
    assert len(p.today_items()) == 3
    item_id = p.today_items()[0]["id"]
    output = json.dumps(
        {"overview": "全貌", "insights": [{"item_id": item_id, "text": "好玩的角度"}]}, ensure_ascii=False
    )
    p.generate = AsyncMock(return_value=output)
    await p.prepare(force=True)
    assert p.generate.await_count >= 4
    assert all(row["digested"] for row in p.today_items())
    assert len((await p.api_today())["data"]["items"]) == 1
    before = p.generate.await_count
    await p.prepare(force=True)
    assert p.generate.await_count == before


async def test_failed_digest_keeps_all_raw_items_pending(tmp_path):
    p = make(tmp_path)
    p.add_item(
        title="材料", summary="原文", url="https://news.example/a", published_at=p.logical_now(), source="f"
    )
    p.generate = AsyncMock(return_value="not json")
    with pytest.raises(json.JSONDecodeError):
        await p.prepare(force=True)
    assert not p.today_items()[0].get("digested")
    assert "digest_at" not in p.store.data


async def test_new_observation_sources_are_always_muted_and_have_tags(tmp_path):
    p = make(tmp_path)
    p.set_plugin_config(
        {
            "plugin": {"config_version": "0.1.0"},
            "observe_groups": [{"group": "group:observed", "tags": ["tech"]}],
        }
    )
    assert p.observation("qq", "observed")
    assert p.source_tags("group:qq:observed") == ["tech"]
    response = await p.guard_send(
        message={"platform": "qq", "message_info": {"group_info": {"group_id": "observed"}}}
    )
    assert response["action"] == "abort"


async def test_optional_search_enrichment_is_bounded_and_material_reaches_digest(tmp_path):
    p = make(tmp_path)
    p.config.batch_max_chars = 10000
    p.config.max_enrich_per_digest = 1
    for i in range(2):
        p.add_item(
            title=f"材料{i}",
            summary="摘要",
            url=f"https://news.example/{i}",
            published_at=p.logical_now(),
            source="f",
        )
    p.call_optional = AsyncMock(side_effect=[None, {"text": "补充的正文"}])
    p.generate = AsyncMock(return_value=json.dumps({"overview": "总览", "insights": []}))
    await p.prepare(force=True)
    calls = [call for call in p.call_optional.await_args_list if call.args[0] == "zeyr.better-search.fetch"]
    assert len(calls) == 1
    assert "补充的正文" in p.generate.await_args_list[0].args[0]
    assert all(row["digested"] for row in p.today_items())

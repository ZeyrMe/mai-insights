"""Small SDK boundary helpers vendored for independent installation."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def result(data: Any = None, *, error: str = "") -> dict:
    return {
        "schema_version": 1,
        "success": not bool(error),
        "data": data,
        "error": error,
        "content": error or json.dumps(data, ensure_ascii=False),
    }


def inject(kwargs: dict, plugin_id: str, text: str, limit: int) -> dict:
    if not text:
        return {"action": "continue"}
    if "items" not in kwargs or kwargs.get("item_schema_version", 1) != 1:
        return {"action": "continue", "modified_kwargs": {**kwargs, "extra_prompt": text[:limit]}}
    identity = f"zeyr:{plugin_id}:context"
    items = [item for item in kwargs["items"] if item.get("meta", {}).get("item_id") != identity]
    items.append(
        {
            "item_type": "UserMessageItem",
            "meta": {
                "item_id": identity,
                "logical_turn_id": None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            "parts": [{"type": "text", "text": text[:limit]}],
        }
    )
    return {"action": "continue", "modified_kwargs": {**kwargs, "items": items}}


class StateFile:
    def __init__(self, path: Path, initial: dict):
        self.path = path
        self.data = json.loads(json.dumps(initial))
        if path.exists():
            loaded = json.loads(path.read_text())
            if loaded.get("schema_version") != initial["schema_version"]:
                raise ValueError("不支持的状态文件版本")
            self.data = loaded

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".state-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(self.data, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

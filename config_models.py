from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator


class Feed(PluginConfigBase):
    id: str = Field(default="", description="稳定来源 ID")
    url: str = Field(default="", description="RSS/RSSHub 地址")
    source_group_id: str = Field(default="", description="观察群来源，可选")
    enabled: bool = Field(default=True, description="启用")


class Route(PluginConfigBase):
    source_group_id: str = Field(default="", description="来源群")
    target_group_ids: list[str] = Field(default_factory=list, description="目标群列表")


class PluginInfo(PluginConfigBase):
    config_version: str = Field(default="0.1.0", description="配置版本，由 Runner 管理")


class Config(PluginConfigBase):
    show_advanced: bool = Field(default=False, description="显示高级设置")
    plugin: PluginInfo = Field(default_factory=PluginInfo)
    enabled: bool = Field(default=True, description="启用插件")
    feeds: list[Feed] = Field(default_factory=list, description="RSS/RSSHub 来源")
    routes: list[Route] = Field(default_factory=list, description="观察群到目标群的定向关系")
    passive_observation_groups: list[str] = Field(default_factory=list, description="只采集不发言的群")
    timezone: str = Field(default="Asia/Shanghai", description="时区")
    day_rollover_hour: int = Field(default=5, ge=0, le=23, description="逻辑日切换小时")
    max_items: int = Field(default=30, ge=1, le=200, description="当天最多保留条目")
    share_cooldown_minutes: int = Field(default=60, ge=1, le=1440, description="分享冷却")
    insight_template: str = Field(default="{title}：{summary}", description="见解渲染模板")
    share_enabled: bool = Field(default=True, description="允许后台定向分享")
    tick_seconds: int = Field(default=60, ge=15, le=3600, description="后台轮询间隔")

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        from zoneinfo import ZoneInfo

        ZoneInfo(value)
        return value


_BASIC_FIELDS = ["enabled", "show_advanced", "feeds", "routes"]
for _name, _field in Config.model_fields.items():
    if _name not in _BASIC_FIELDS and _name != "plugin":
        _field.json_schema_extra = {
            **(_field.json_schema_extra or {}),
            "depends_on": "show_advanced",
            "depends_value": True,
        }
Config.model_rebuild(force=True)

from maibot_sdk import MaiBotPlugin

from .config_models import Config


class Plugin(MaiBotPlugin):
    config_model = Config

    async def on_load(self):
        self.ctx.logger.info("开发骨架已加载，功能尚未开放")

    async def on_unload(self):
        pass

    async def on_config_update(self, scope, config_data, version):
        pass


def create_plugin():
    return Plugin()

from __future__ import annotations
from datetime import timedelta
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from .data_client import (
    StateGridDataClient,
    STATE_MAINTENANCE,
    STATE_WAF,
)
from .const import DOMAIN
from .utils.logger import LOGGER

# 维护/风控期间拉长轮询间隔（仅做轻量探测，不触发登录/消耗 LLM）
_MAINTENANCE_INTERVAL = timedelta(seconds=900)
_NORMAL_INTERVAL = timedelta(seconds=300)


class StateGridCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=_NORMAL_INTERVAL,
        )
        self.data_client: StateGridDataClient = hass.data[DOMAIN]

    async def _async_update_data(self):
        # 智能判断是否需要强制刷新：
        # - 首次安装（powerUserList 为空）：必须强制刷新，否则永远拉不到数据
        # - 重启场景（有缓存数据）：不强制刷新，由 refresh_data 内部 12 小时判断决定
        #   这样可以避免重启就触发 API 调用，消耗 RK001 日额度
        has_cached_data = bool(self.data_client.powerUserList)
        force_refresh = not has_cached_data
        await self.data_client.refresh_data(force_refresh=force_refresh)

        # 智能更新识别维护/风控状态：维护期间拉长轮询间隔，避免无谓重试
        state = self.data_client.last_status.get("state")
        if state in (STATE_MAINTENANCE, STATE_WAF):
            self.update_interval = _MAINTENANCE_INTERVAL
        else:
            self.update_interval = _NORMAL_INTERVAL

        return self.data_client.get_door_account()

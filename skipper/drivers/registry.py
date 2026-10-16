"""Driver 注册表：名字 → driver 实例。"""
from __future__ import annotations

from typing import Dict, List, Optional

from skipper.config import Settings, get_settings
from skipper.drivers.base import AgentDriver
from skipper.drivers.opencode import OpencodeDriver
from skipper.drivers.self_loop import SelfLoopDriver


class DriverRegistry:
    """统一管理可插拔 driver。

    用法：
        registry = DriverRegistry(berth_client, llm_client)
        driver = registry.get("self_loop")
        registry.list()  # [{"name": "self_loop", ...}, {"name": "opencode", ...}]
    """

    def __init__(
        self,
        berth_client,
        llm_client=None,
        *,
        settings: Optional[Settings] = None,
        drivers: Optional[List[AgentDriver]] = None,
    ):
        self.berth = berth_client
        self.llm = llm_client
        self.settings = settings or get_settings()
        self._drivers: Dict[str, AgentDriver] = {}

        self.register(SelfLoopDriver(berth_client, llm_client, settings=self.settings))
        self.register(OpencodeDriver(berth_client, settings=self.settings))
        for extra in drivers or []:
            self.register(extra)

    def register(self, driver: AgentDriver) -> None:
        """注册（或覆盖）一个 driver。"""
        self._drivers[driver.name()] = driver

    def get(self, name: str) -> AgentDriver:
        if name not in self._drivers:
            available = sorted(self._drivers)
            raise KeyError(f"未知 driver: {name!r}（可用: {available}）")
        return self._drivers[name]

    def has(self, name: str) -> bool:
        return name in self._drivers

    def names(self) -> List[str]:
        return sorted(self._drivers)

    def list(self) -> List[dict]:
        return [driver.describe() for driver in self._drivers.values()]

    @property
    def default_name(self) -> str:
        if self.settings.default_driver in self._drivers:
            return self.settings.default_driver
        return self.names()[0] if self._drivers else ""

    def __len__(self) -> int:
        return len(self._drivers)

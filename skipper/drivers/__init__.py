"""drivers：可插拔的 Agent 执行器。"""
from skipper.drivers.base import AgentDriver
from skipper.drivers.opencode import OpencodeDriver
from skipper.drivers.registry import DriverRegistry
from skipper.drivers.self_loop import SelfLoopDriver

__all__ = ["AgentDriver", "SelfLoopDriver", "OpencodeDriver", "DriverRegistry"]

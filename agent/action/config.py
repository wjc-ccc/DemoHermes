"""Action 的启动配置；工具集变更在下一次启动生效。"""
from pydantic import BaseModel, ConfigDict, Field
from .policy import Toolset


class ActionConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    default_toolset: str = "default"
    toolsets: tuple[Toolset, ...] = ()
    max_read_workers: int = Field(default=4, ge=1)
    lease_ms: int = Field(default=60000, ge=1)
    patch_cap: int = Field(default=8, ge=0)
    segment_retries: int = Field(default=2, ge=0)


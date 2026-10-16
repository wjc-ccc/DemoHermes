"""perception / admission 数据契约。"""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


AdmissionMode = Literal["off", "shadow", "active"]


AdmissionAction = Literal["keep", "trim", "defer"]


class AdmissionBand(BaseModel):
    """用量达到 usage 后生效的一档阈值：score<=defer_below 移出，<trim_below 裁剪。"""

    model_config = ConfigDict(frozen=True)

    usage: float = Field(gt=0.0, le=1.0)
    defer_below: float = Field(ge=0.0, le=1.0)
    trim_below: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _ordered(self) -> "AdmissionBand":
        """defer 必须比 trim 更严。"""
        if self.defer_below > self.trim_below:
            raise ValueError("defer_below 不能大于 trim_below")
        return self


_DEFAULT_BANDS = (
    AdmissionBand(usage=0.50, defer_below=0.15, trim_below=0.50),
    AdmissionBand(usage=0.70, defer_below=0.25, trim_below=0.60),
    AdmissionBand(usage=0.85, defer_below=0.35, trim_below=0.70),
)


### admission的策略 如何设定admit的处理参数（包含jev参数 和 处理字段参数）
class AdmissionConfig(BaseModel):
    """准入配置：开关、档位、裁剪长度，以及判官和先验的混合权重。"""

    model_config = ConfigDict(frozen=True)

    mode: AdmissionMode = "off"
    # 按 usage 升序；低于第一档不产生新定案。
    bands: tuple[AdmissionBand, ...] = _DEFAULT_BANDS
    # 短于该字数的工具结果不值得判。
    min_chars: int = Field(default=2000, ge=1)
    # 至少隔这么多个 user 轮次才可判；1 表示只判此前的轮次。
    min_age_turns: int = Field(default=1, ge=1)
    # 判官看到的结果开头字数。
    judge_head_chars: int = Field(default=600, ge=1)
    # trim 保留的首尾各多少字。
    edge_chars: int = Field(default=600, ge=1)
    # 单次构建最多送判的条目数，按字数从大到小。
    max_judged: int = Field(default=24, ge=0)
    # 当前 Agent 是否注册了 memory_read；没有时占位符改为提示重新执行。
    readback: bool = True
    # 有判官：judge_weight * 仍需要的概率 + (1 - judge_weight) * 相关度。
    judge_weight: float = Field(default=0.8, ge=0.0, le=1.0)
    # 判官缺席：prior_weight * 类别先验 + (1 - prior_weight) * 相关度。
    prior_weight: float = Field(default=0.6, ge=0.0, le=1.0)
    trim_min_saving: int = Field(default=200, ge=0)
    min_kind_probability: float = Field(default=0.5, gt=0.0, le=1.0)
    prior_error: float = Field(default=0.30, ge=0.0, le=1.0)
    prior_listing: float = Field(default=0.30, ge=0.0, le=1.0)
    prior_log: float = Field(default=0.25, ge=0.0, le=1.0)
    prior_content: float = Field(default=0.55, ge=0.0, le=1.0)
    prior_data: float = Field(default=0.65, ge=0.0, le=1.0)
    prior_other: float = Field(default=0.50, ge=0.0, le=1.0)

    def prior(self, kind: str | None) -> float:
        return {
            "error": self.prior_error,
            "listing": self.prior_listing,
            "log": self.prior_log,
            "content": self.prior_content,
            "data": self.prior_data,
        }.get(kind or "", self.prior_other)

    @model_validator(mode="after")
    def _sorted_bands(self) -> "AdmissionConfig":
        """档位必须按用量严格升序。"""
        usages = [band.usage for band in self.bands]
        if usages != sorted(set(usages)):
            raise ValueError("bands 必须按 usage 严格升序")
        return self


AdmissionPolicy = AdmissionConfig

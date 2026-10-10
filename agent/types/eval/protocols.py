"""eval / protocols 数据契约。"""
from __future__ import annotations

from agent.types.eval.cases import EvalSuite
from agent.types.eval.reports import EvalReport


class EvalRunnerLike:
    def run(self, suite: EvalSuite) -> EvalReport: ...

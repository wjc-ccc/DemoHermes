"""上下文准入的判官 Adapter：只回答有界问题并给概率，失败由准入层兜底。

判官看到的是小状态（调用描述、结果开头、字数、轮龄），不是正文全文。
SystemOneContextJudge 只实现 System One 协议，端点、模型、密钥全部注入；
具体用哪个 provider 由 agent/core/provider.py 的 build_context_judge 决定。
替换 provider 时只需实现 ContextJudge 协议。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import math
from typing import Sequence

import httpx

from agent.core.perception.errors import ContextJudgeError
from agent.types.perception.judge import ContextJudgeRequest, ContextJudgeResult

# 选择题必须带逃生选项 other，否则无关输入也会被高置信地塞进某一类。
KIND_CRITERIA = {
    "error": "An error or failure message",
    "listing": "File names, search hits or a list of matches",
    "content": "Source code or document text",
    "log": "Build, test, command or progress output",
    "data": "Facts or values looked up for the task",
    "other": "Something else",
}
_MAX_ERROR_DETAIL = 300
_MAX_BATCH_ITEMS = 4  # System One 每次最多 8 道题，每条结果占 choice + noul 两题。


class SystemOneContextJudge:
    """System One 协议判官：一批条目共享一份 state，每条两道题，一次请求。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 6.0,
        batch_size: int = 4,
        max_workers: int = 4,
        min_kind_probability: float = 0.5,
        client: httpx.Client | None = None,
    ) -> None:
        """端点、模型、密钥由 provider 配置注入；client 仅供测试替换传输层。"""
        if not api_key or not base_url or not model:
            raise ValueError("SystemOneContextJudge 需要 api_key、base_url 和 model")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._batch_size = max(1, min(batch_size, _MAX_BATCH_ITEMS))
        self._max_workers = max(1, max_workers)
        if isinstance(min_kind_probability, bool) or not isinstance(min_kind_probability, (int, float)) or not 0 < float(min_kind_probability) <= 1:
            raise ValueError("min_kind_probability 必须在 (0, 1] 内")
        self._min_kind_probability = float(min_kind_probability)
        self._client = client
        self.id = f"systemone:{httpx.URL(self._base_url).host}:{self._model}"

    def judge(
        self, requests: Sequence[ContextJudgeRequest]
    ) -> list[ContextJudgeResult]:
        """按 batch_size 切批并发请求；任一批失败即整体失败，交给兜底。"""
        if not requests:
            return []
        batches = [
            list(requests[start:start + self._batch_size])
            for start in range(0, len(requests), self._batch_size)
        ]
        if len(batches) == 1:
            answered = [self._judge_batch(batches[0])]
        else:
            with ThreadPoolExecutor(
                max_workers=min(self._max_workers, len(batches))
            ) as pool:
                answered = list(pool.map(self._judge_batch, batches))
        return [result for batch in answered for result in batch]

    def _judge_batch(
        self, batch: Sequence[ContextJudgeRequest]
    ) -> list[ContextJudgeResult]:
        """发送一批 System One 请求并逐题解析。"""
        state: dict[str, object] = {"goal": batch[0].goal}
        questions: dict[str, dict] = {}
        for index, request in enumerate(batch, start=1):
            field = f"i{index}"
            state[field] = {
                "call": request.call,
                "result": request.result_head,
                "result_chars": request.result_chars,
                "age_turns": request.age_turns,
                "tool_calls_since": request.calls_since,
            }
            questions[f"k{index}"] = {
                "type": "choice",
                "instructions": f"What is `{field}.result`?",
                "criteria": KIND_CRITERIA,
            }
            questions[f"n{index}"] = {
                "type": "noul",
                "instructions": (
                    f"Is the full text of `{field}.result` still needed to finish `goal`?"
                ),
            }
        body = self._post({"model": self._model, "state": state, "questions": questions})
        answers = _normalize_answers(body.get("answers"), questions)
        if not isinstance(answers, dict):
            raise ContextJudgeError("invalid_response", "判官响应缺少 answers")
        return [
            ContextJudgeResult(
                item_id=request.item_id,
                kind=_kind_of(answers.get(f"k{index}"), min_probability=self._min_kind_probability),
                still_needed=_probability_of(answers.get(f"n{index}")),
            )
            for index, request in enumerate(batch, start=1)
        ]

    def _post(self, payload: dict) -> dict:
        """POST 到 System One 端点；错误信息不含密钥与状态正文。"""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            if self._client is not None:
                response = self._client.post(
                    self._base_url, json=payload, headers=headers, timeout=self._timeout
                )
            else:
                response = httpx.post(
                    self._base_url, json=payload, headers=headers, timeout=self._timeout
                )
        except httpx.TimeoutException as exc:
            raise ContextJudgeError("timeout", type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            raise ContextJudgeError("unreachable", type(exc).__name__) from exc
        if response.status_code >= 400:
            raise ContextJudgeError(
                f"http_{response.status_code}", _error_message(response)
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ContextJudgeError("invalid_response", "响应不是 JSON") from exc
        if not isinstance(body, dict):
            raise ContextJudgeError("invalid_response", "响应不是对象")
        return body


def _kind_of(raw: object, *, min_probability: float = 0.5) -> str | None:
    """取选中类别；逃生选项或概率过低视为没有答案。"""
    if not isinstance(raw, dict):
        return None
    choice = raw.get("choice")
    if not isinstance(choice, str) or choice not in KIND_CRITERIA or choice == "other":
        return None
    probabilities = raw.get("probabilities")
    if isinstance(probabilities, dict):
        probability = probabilities.get(choice)
        if (isinstance(probability, bool) or not isinstance(probability, (int, float))
                or not math.isfinite(probability) or probability < min_probability):
            return None
    return choice


def _probability_of(raw: object) -> float | None:
    """System One 的是非题答案字段叫 noul，取值为 P(true)。"""
    if not isinstance(raw, dict):
        return None
    value = raw.get("noul")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return min(1.0, max(0.0, float(value)))


def _error_message(response: httpx.Response) -> str:
    """读取 {error:{message}} / {error} / {message} / {detail} 中的错误说明。"""
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    message = ""
    if isinstance(body, dict):
        nested = body.get("error")
        if isinstance(nested, dict) and isinstance(nested.get("message"), str):
            message = nested["message"]
        elif isinstance(nested, str):
            message = nested
        elif isinstance(body.get("message"), str):
            message = body["message"]
        elif isinstance(body.get("detail"), str):
            message = body["detail"]
    return (message or f"HTTP {response.status_code}")[:_MAX_ERROR_DETAIL]


def _normalize_answers(answers, questions):
    """统一判官返回的结构差异；不猜测缺失或冲突分数，不改变置信门槛。"""
    if not isinstance(answers, dict):
        return answers
    normalized = {}
    for key, question in questions.items():
        candidates = []
        if key in answers:
            candidates.append(answers[key])
        item = answers.get("i" + key[1:])
        if isinstance(item, dict):
            if key in item:
                candidates.append(item[key])
            if question["type"] == "choice" and "choice" in item:
                candidates.append({k: item[k] for k in ("choice", "probabilities") if k in item})
        if question["type"] == "noul":
            candidates = [{"noul": value} if type(value) in (int, float) and math.isfinite(value)
                          else value for value in candidates]
        if candidates:
            # 同一题的平铺和嵌套答案冲突时保持未知，不能随便取一个数值。
            normalized[key] = candidates[0] if all(value == candidates[0] for value in candidates) else None
    return normalized

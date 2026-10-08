"""卡片主张与引用原文的轻量一致性检查。

职责：复用词语重合和名称、数字锚点校验。
边界：这是保守过滤，不证明自然语言语义蕴含；来源授权由调用方完成。
关联：MemoryBatchReviewer 和显式卡片 Mutation 使用。
"""
from __future__ import annotations

import re


_LATIN_TERMS = re.compile(r"[a-z0-9_./+#-]{3,}", re.IGNORECASE)
_HAN_TERMS = re.compile(r"[\u3400-\u9fff]{2,}")
_CLAIM_ANCHORS = re.compile(r"\b[A-Z][A-Za-z0-9_.-]{2,}\b|\b\d+(?:\.\d+)*\b")
_GENERIC_TERMS = {"用户", "助手", "项目", "事情", "问题", "经验", "信息",
                  "内容", "使用", "记住", "以后", "回答", "偏好"}

_NEGATED_SUCCESS = re.compile(
    r"(?:尚未|仍未|未能|没有|并未|尚无|并无|无法|没能|未|不)"
    r"(?:找到|获得|取得|实现|达到|验证|确认|证明|证实|真正|完全|全部|任何|可以|能够|能|曾|有|一定|算作|算是){0,4}(?:成功|通过)"
    r"|\b(?:not|never|no)\s+(?:(?:yet|been|a|any)\s+)*"
    r"(?:success(?:ful|fully)?|passed|succeeded)\b", re.I,
)
_POSITIVE_OUTCOME = re.compile(r"成功|通过|\b(?:success(?:ful|fully)?|passed|succeeded)\b", re.I)
_NEGATIVE_OUTCOME = re.compile(r"失败|报错|\b(?:failed|failure|error)\b", re.I)


_EXPLICIT_OPENINGS = {
    "profile": ("我叫", "我是", "我的名字", "我会", "我擅长"),
    "stable_preference": ("以后", "请用", "我喜欢", "我偏好", "我习惯", "不要", "改成"),
    "ongoing_fact": ("我正在", "我目前", "我的项目", "我在做", "我们正在"),
}
# 更正、遗忘等请求常以“更正一下：”“我改主意了：”开头，不能只看句首。
_EXPLICIT_REQUESTS = ("记住", "更正", "纠正", "我改主意", "改成", "改为", "以后",
                      "不要再", "忘掉", "忘记", "删除", "删掉", "我使用")


def explicit_user_statement(text: str, kind: str | None = None) -> bool:
    """用户原文是否明确陈述或要求修改；kind 为空时接受任一类别的句首陈述。"""
    text = text.strip()
    if any(marker in text for marker in _EXPLICIT_REQUESTS):
        return True
    openings = _EXPLICIT_OPENINGS.get(kind, ()) if kind else tuple(
        marker for markers in _EXPLICIT_OPENINGS.values() for marker in markers)
    return any(text.startswith(marker) for marker in openings)


def experience_outcome_signals(text: str) -> tuple[bool, bool]:
    """提取轻量成败线索；先排除否定的成功表达，避免把“未找到成功方案”误判成成功。

    只在同一短分句中识别有限的否定形式，不跨标点吞掉后续真实成功结果。
    这是 Reviewer 与 Validator 共用的保守检查，不替代完整语义判断。
    """
    affirmative, negated_count = _NEGATED_SUCCESS.subn("", text)
    return bool(_POSITIVE_OUTCOME.search(affirmative)), bool(negated_count or _NEGATIVE_OUTCOME.search(text))


def has_source_overlap(claim: str, evidence: str) -> bool:
    source = evidence.casefold()
    terms = [word.casefold() for word in _LATIN_TERMS.findall(claim)]
    terms.extend(segment[index:index + 2] for segment in _HAN_TERMS.findall(claim)
                 for index in range(len(segment) - 1))
    return any(term not in _GENERIC_TERMS and term in source for term in terms)


_HAN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
               "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_HAN_NUMBER = re.compile(r"[零〇一二两三四五六七八九十百]+")


def _han_number(match: re.Match) -> str:
    token = match.group()
    if not set(token) & {"十", "百"}:
        return "".join(str(_HAN_DIGITS[char]) for char in token)
    total = current = 0
    for char in token:
        if char in _HAN_DIGITS:
            current = _HAN_DIGITS[char]
        else:
            total += (current or 1) * (10 if char == "十" else 100)
            current = 0
    return str(total + current)


def unsupported_anchors(claim: str, evidence: str) -> tuple[str, ...]:
    # 卡片常把原文“两秒”写成“2 秒”，数字锚点也接受原文中的中文数字。
    quoted = evidence.casefold() + "\n" + _HAN_NUMBER.sub(_han_number, evidence)
    return tuple(anchor for anchor in set(_CLAIM_ANCHORS.findall(claim))
                 if anchor.casefold() not in quoted)


"""
CalculatorTool — 四则运算计算器（测试用工具）

用于验证「LLM 发起工具调用 → Registry 分发 → 结果回注」完整链路。
出于安全考虑不用 eval：用 ast 解析表达式，只放行白名单节点
（数字、加减乘除、取整、取余、幂、括号、abs/round/min/max/sqrt）。
"""
from __future__ import annotations

import ast
import math
import operator

from .base import Tool, ToolResult

# 允许的二元运算符
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

# 允许的一元运算符
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}

# 允许的白名单函数
_FUNCS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
}


def _safe_eval(node: ast.AST) -> float:
    """递归求值 AST；遇到白名单外的节点直接抛 ValueError。"""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"不支持的常量: {node.value!r}")
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_safe_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
        args = [_safe_eval(a) for a in node.args]
        return _FUNCS[node.func.id](*args)
    raise ValueError(f"表达式包含不允许的内容: {ast.dump(node)}")


class CalculatorTool(Tool):
    name = "calculator"
    description = (
        "计算数学表达式的值。支持 + - * / // % ** 、括号以及 "
        "abs/round/min/max/sqrt 函数。需要精确计算时务必调用本工具，不要口算。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "要计算的数学表达式，例如 (3+5)*2 或 sqrt(16)+2**3",
            }
        },
        "required": ["expression"],
    }

    def execute(self, arguments: dict) -> ToolResult:
        expression = str(arguments.get("expression", "")).strip()
        if not expression:
            return ToolResult(ok=False, error="缺少参数 expression")
        try:
            value = _safe_eval(ast.parse(expression, mode="eval"))
        except Exception as e:
            return ToolResult(ok=False, error=f"表达式无法计算: {e}")
        # 整数结果去掉 .0，阅读更友好
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return ToolResult(
            ok=True,
            result_text=f"{expression} = {value}",
            data={"expression": expression, "value": value},
        )

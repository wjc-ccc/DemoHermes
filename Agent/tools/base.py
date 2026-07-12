"""
Tool — 工具抽象基类

所有工具必须实现此接口：
    name        : 工具名称（如 "read_file"）
    description : 工具描述（供 LLM 理解用途）
    schema      : JSON Schema 参数定义
    execute()   : 执行工具，返回结果字符串

LLM 通过 schema 决定何时、如何调用工具。
"""

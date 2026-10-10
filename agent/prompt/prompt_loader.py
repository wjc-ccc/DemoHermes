"""读取项目当前维护的提示词定义。"""
from agent.prompt.prompts.systemPrompt import systemPrompt


def load_system_prompt() -> str:
    """返回当前系统提示词；提示词正文只在 prompts 中维护。"""
    return systemPrompt

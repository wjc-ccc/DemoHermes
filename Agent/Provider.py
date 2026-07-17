"""
全局配置中心

从项目根目录的 .env 文件加载所有环境变量，供其他模块统一引用。

使用方式：
    from Agent.provider import DEEPSEEK_API_KEY, MODEL
"""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]  # 项目根目录

load_dotenv(ROOT / ".env")

# DeepSeek 配置
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# Anthropic / Claude 配置
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")

# 默认使用的模型名称
MODEL = os.getenv("MODEL", "deepseek-v4-flash")

# System Prompt 文件路径（由 ContextBuilder 加载）
SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "prompt" / "systemPrompt.md"

# Channel 配置文件路径
CHANNEL_CONFIG = os.getenv("CHANNEL_CONFIG")
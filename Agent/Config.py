"""
全局配置中心

从项目根目录的 .env 文件加载所有环境变量，供其他模块统一引用。
避免各文件重复 load_dotenv，参考 ErrorDocs/7.10 debug日志.md。

使用方式：
    from Agent.Config import DEEPSEEK_API_KEY, MODEL
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

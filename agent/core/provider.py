"""
全局配置中心

从项目根目录的 .env 文件加载所有环境变量，供其他模块统一引用。

使用方式：
    from agent.core.provider import DEEPSEEK_API_KEY, MODEL
"""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]  # 项目根目录

load_dotenv(ROOT / ".env")

# DeepSeek 配置
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# Anthropic / Claude 配置
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")

# 字节方舟 Agent Plan（豆包）。协议是 Anthropic，密钥和地址与上面两套分开。
DOUBAO_API_KEY = os.getenv("DOUBAO_API_KEY")
DOUBAO_BASE_URL = os.getenv("DOUBAO_BASE_URL", "https://ark.cn-beijing.volces.com/api/plan")

# 选用哪一套密钥：deepseek | claude | doubao。模型名仍由 MODEL 指定。
MODEL_PROVIDER = os.getenv("MODEL_PROVIDER", "deepseek").strip().lower()

# 默认使用的模型名称
MODEL = os.getenv("MODEL", "deepseek-v4-flash")

# 上下文准入判官（jevmodel.org 的 System One）；没有 key 时准入只用规则与先验
# POST {base}/  body: {state, model, questions}  header: Authorization: Bearer <JEVMODEL_API_KEY>
JEVMODEL_BASE_URL = "https://jevmodel.org/v1/systemone"
JEV_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/systemone"
# 各端点上 Jev 的模型名不同；未知端点回退 jev-latest。
_JEV_MODEL_BY_BASE_URL = {
    JEVMODEL_BASE_URL: "jev-latest",
    JEV_OPENROUTER_BASE_URL: "~typesafe/jev-latest",
}
JEVMODEL_API_KEY = os.getenv("JEVMODEL_API_KEY")
JEV_BASE_URL = (os.getenv("JEV_BASE_URL") or JEVMODEL_BASE_URL).rstrip("/")
JEV_MODEL = os.getenv("JEV_MODEL") or _JEV_MODEL_BY_BASE_URL.get(JEV_BASE_URL, "jev-latest")
JEV_TIMEOUT = float(os.getenv("JEV_TIMEOUT", "6"))
JEV_BATCH_SIZE = int(os.getenv("JEV_BATCH_SIZE", "4"))
CONTEXT_ADMISSION_MODE = os.getenv("CONTEXT_ADMISSION_MODE", "shadow").strip().lower()

# 前端 HTTP API 访问控制：配置 token 后每个 /api 请求都要带 Authorization: Bearer <token>
AGENT_HTTP_TOKEN = os.getenv("AGENT_HTTP_TOKEN") or None
AGENT_HTTP_ALLOWED_ORIGINS = tuple(
    origin.strip()
    for origin in os.getenv(
        "AGENT_HTTP_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if origin.strip()
)

# System Prompt 文件路径（由 PerceptionBuilder 的加载阶段读取）
SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "prompt" / "systemPrompt.py"

# Channel 配置文件路径
def _resolve_channel_config() -> str:
    """CHANNEL_CONFIG 未设置或路径失效时，回退到包内 channel/registry.json。"""
    env = os.getenv("CHANNEL_CONFIG")
    if env and Path(env).exists():
        return env
    return str(Path(__file__).resolve().parent / "channel" / "registry.json")


CHANNEL_CONFIG = _resolve_channel_config()


def build_context_judge(*, timeout=None, batch_size=None, max_workers=None, min_kind_probability=None):
    """按 JEVMODEL_API_KEY 构造准入判官；没有 key 返回 None，准入退回规则 + 先验。"""
    if not JEVMODEL_API_KEY:
        return None
    # perception 加载时会反向导入本模块，这里必须延迟导入。
    from agent.core.perception.perception_judge import SystemOneContextJudge

    return SystemOneContextJudge(
        api_key=JEVMODEL_API_KEY,
        base_url=JEV_BASE_URL,
        model=JEV_MODEL,
        timeout=JEV_TIMEOUT if timeout is None else timeout,
        batch_size=JEV_BATCH_SIZE if batch_size is None else batch_size,
        **({"max_workers": max_workers} if max_workers is not None else {}),
        **({"min_kind_probability": min_kind_probability} if min_kind_probability is not None else {}),
    )


def build_admission_policy():
    """按 CONTEXT_ADMISSION_MODE 构造准入策略；非法取值按 shadow 处理。"""
    from agent.types.perception.admission import AdmissionPolicy

    mode = CONTEXT_ADMISSION_MODE if CONTEXT_ADMISSION_MODE in {"off", "shadow", "active"} else "shadow"
    return AdmissionPolicy(mode=mode)


def build_experience_judge(config=None):
    """事后经验评判使用独立协议；未配置密钥时只积累待评记录。"""
    from agent.core.memory.config import MemoryExperienceConfig
    from agent.core.memory.memory_experience_judge import SystemOneExperienceJudge

    config = config or MemoryExperienceConfig()
    key = os.getenv("MEMORY_JEV_API_KEY") or JEVMODEL_API_KEY
    if not config.enabled or not key:
        return None
    base_url = (os.getenv("MEMORY_JEV_BASE_URL") or JEV_BASE_URL).rstrip("/")
    model = os.getenv("MEMORY_JEV_MODEL") or _JEV_MODEL_BY_BASE_URL.get(base_url, JEV_MODEL)
    return SystemOneExperienceJudge(
        api_key=key, base_url=base_url, model=model, timeout=JEV_TIMEOUT,
        batch_size=config.judge_batch_size, max_input_tokens=config.judge_input_tokens,
    )

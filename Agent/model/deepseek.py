"""
DeepSeek Provider — DeepSeek 模型接入与连通性测试

通过 OpenAI SDK（兼容接口）调用 DeepSeek API。
当前为独立测试脚本，后续将重构为继承 BaseModel 的 Provider 类。

运行测试：python -m Agent.model.deepseek

注意：API Key 应统一从 Agent.Config 读取，避免重复 load_dotenv。
"""
import os
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def test():
    """连通性测试：发送一条简单消息，验证 API 可用。"""
    client = OpenAI(
        api_key=os.environ.get('DEEPSEEK_API_KEY'),
        base_url="https://api.deepseek.com")

    response = client.chat.completions.create(
        model="deepseek-v4-flash",
        messages=[
            {"role": "system", "content": "你是一个测试版本的agent loop，用户问什么你简单回答即可"},
            {"role": "user", "content": "你好"},
        ],
        stream=False,
        reasoning_effort="high",
        extra_body={"thinking": {"type": "enabled"}}
    )

    print(response.choices[0].message.content)


if __name__ == "__main__":
    test()

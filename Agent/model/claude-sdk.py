import asyncio
from pathlib import Path
from dotenv import load_dotenv
from claude_agent_sdk import query, ClaudeAgentOptions

ROOT = Path(__file__).resolve().parents[2]  #上两级到项目根
load_dotenv(ROOT / ".env")









## demo
def test(choice):
    async def singleStep():
        async for message in query(
            prompt="你好",
            options=ClaudeAgentOptions(allowed_tools=["Read", "Edit", "Bash"]),
        ):
            print(message)

    async def multiStep():
        pass

    if choice == "single_test":
        asyncio.run(singleStep())
    else:
        pass

if __name__ == "__main__":
    test("single_test")
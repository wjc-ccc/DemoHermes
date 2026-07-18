"""
一键启动后端进程

用法：
    python main.py            # HTTP 模式：作为 frontier 前端的后端（:8000）
    python main.py --cli      # 终端对话模式
    python main.py --port 9000

前端启动：python frontier/main.py
"""
from agent.main import main

if __name__ == "__main__":
    main()

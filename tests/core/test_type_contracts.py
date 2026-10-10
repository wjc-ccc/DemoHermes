"""公开协议可独立导入，不应触发引擎、SDK 或存储装配。"""
from pathlib import Path
import subprocess
import sys
import textwrap


def test_contracts_import_without_loading_implementations():
    script = textwrap.dedent("""
        import importlib
        import pkgutil
        import sys
        import agent.types

        for module in pkgutil.walk_packages(agent.types.__path__, agent.types.__name__ + '.'):
            importlib.import_module(module.name)

        forbidden = []
        for name in sys.modules:
            if name.startswith((
                'agent.gateway', 'agent.storage', 'agent.model', 'agent.tools',
                'agent.core.action', 'agent.eval', 'agent.evolution', 'agent.skills', 'agent.provider',
                'httpx', 'openai', 'claude_agent_sdk', 'lark_oapi',
            )):
                forbidden.append(name)
            elif name.startswith('agent.core'):
                forbidden.append(name)
        assert not forbidden, forbidden
    """)
    result = subprocess.run(
        [sys.executable, '-B', '-c', script],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr

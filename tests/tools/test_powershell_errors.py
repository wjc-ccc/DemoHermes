"""真实Windows shell的错误必须与进程退出状态保持一致。"""
import os

import pytest

from agent.core.tools.bash_tools import BashTool
from agent.types.tools import ToolInvocationContext


@pytest.mark.skipif(os.name != 'nt', reason='仅验证Windows PowerShell封装')
@pytest.mark.parametrize(('command', 'expected_ok', 'exit_code'), [
    ("Get-Item -LiteralPath './missing-real-tool-regression'", False, 1),
    ('Write-Output 42', True, 0),
    ('python -c "import sys; sys.exit(7)"', False, 7),
])
def test_real_shell_error_status(tmp_path, command, expected_ok, exit_code):
    result = BashTool().execute(
        {'command': command, 'workdir': str(tmp_path), 'timeout': 10000},
        context=ToolInvocationContext(session_id='powershell-error-regression'),
    )
    assert result.ok is expected_ok, result.model_dump()
    assert result.data['exit_code'] == exit_code

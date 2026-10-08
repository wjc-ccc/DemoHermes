"""The QR login only changes its own .env keys after confirmation."""
from scripts.wechat_qr_login import _save_credentials


def test_save_credentials_preserves_other_settings(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "MODEL=deepseek-v4-flash\nWEIXIN_TOKEN=old\nWEIXIN_ACCOUNT_ID=old\n",
        encoding="utf-8",
    )
    _save_credentials(env, {
        "WEIXIN_TOKEN": "new-token",
        "WEIXIN_ACCOUNT_ID": "new-account",
        "WEIXIN_BASE_URL": "https://ilinkai.weixin.qq.com",
    })
    content = env.read_text(encoding="utf-8")
    assert "MODEL=deepseek-v4-flash" in content
    assert content.count("WEIXIN_TOKEN=") == 1
    assert 'WEIXIN_TOKEN="new-token"' in content
    assert 'WEIXIN_ACCOUNT_ID="new-account"' in content
    assert 'WEIXIN_BASE_URL="https://ilinkai.weixin.qq.com"' in content

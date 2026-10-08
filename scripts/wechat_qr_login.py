"""Log in to the personal WeChat iLink Bot and save credentials to .env.

Run with ``python scripts/wechat_qr_login.py``. The QR code is generated locally;
the token and account ID are written only after confirmation in WeChat.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import httpx
import qrcode


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://ilinkai.weixin.qq.com"
HEADERS = {"iLink-App-Id": "bot", "iLink-App-ClientVersion": str((2 << 16) | (2 << 8))}


def _get(client: httpx.Client, base_url: str, endpoint: str, params: dict[str, str]) -> dict:
    response = client.get(f"{base_url.rstrip('/')}/{endpoint}", params=params, headers=HEADERS)
    response.raise_for_status()
    return response.json()


def _new_qr(client: httpx.Client, qr_path: Path) -> str:
    result = _get(client, BASE_URL, "ilink/bot/get_bot_qrcode", {"bot_type": "3"})
    qr_id = str(result.get("qrcode") or "")
    scan_content = str(result.get("qrcode_img_content") or qr_id)
    if not qr_id or not scan_content:
        raise RuntimeError("iLink did not return a QR code")
    qr_path.parent.mkdir(parents=True, exist_ok=True)
    qrcode.make(scan_content, box_size=10, border=4).save(qr_path)
    print(f"QR_READY {qr_path}", flush=True)
    return qr_id


def _save_credentials(env_path: Path, values: dict[str, str]) -> None:
    """Replace only WeChat settings, preserving the user's other .env values."""
    original = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    lines = original.splitlines(keepends=True)
    for key, value in values.items():
        replacement = f"{key}={json.dumps(value, ensure_ascii=False)}\n"
        pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
        matches = [index for index, line in enumerate(lines) if pattern.match(line)]
        if matches:
            lines[matches[0]] = replacement
            for index in reversed(matches[1:]):
                del lines[index]
        else:
            lines.append(replacement)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".env.weixin.", dir=env_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.writelines(lines)
        os.replace(temp_name, env_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def login(env_path: Path, qr_path: Path, timeout_seconds: int) -> int:
    deadline = time.monotonic() + timeout_seconds
    with httpx.Client(timeout=40.0, trust_env=True) as client:
        qr_id = _new_qr(client, qr_path)
        base_url = BASE_URL
        last_status = ""
        refreshes = 0
        while time.monotonic() < deadline:
            try:
                result = _get(client, base_url, "ilink/bot/get_qrcode_status", {"qrcode": qr_id})
            except httpx.TimeoutException:
                continue
            except httpx.HTTPError as exc:
                # Exception URLs contain the QR token: never print them.
                print(f"POLL_ERROR {type(exc).__name__}", flush=True)
                time.sleep(2)
                continue
            status = str(result.get("status") or "wait")
            if status != last_status:
                print(f"STATUS {status}", flush=True)
                last_status = status
            if status == "scaned_but_redirect":
                host = str(result.get("redirect_host") or "")
                if host:
                    base_url = f"https://{host}"
            elif status == "expired":
                refreshes += 1
                if refreshes > 3:
                    print("QR_EXPIRED", flush=True)
                    return 1
                qr_id = _new_qr(client, qr_path)
                base_url = BASE_URL
                last_status = ""
            elif status == "confirmed":
                account_id = str(result.get("ilink_bot_id") or "")
                token = str(result.get("bot_token") or "")
                if not account_id or not token:
                    print("CONFIRMED_MISSING_CREDENTIALS", flush=True)
                    return 1
                _save_credentials(env_path, {
                    "WEIXIN_TOKEN": token,
                    "WEIXIN_ACCOUNT_ID": account_id,
                    "WEIXIN_BASE_URL": str(result.get("baseurl") or base_url),
                })
                qr_path.unlink(missing_ok=True)
                print("CONNECTED credentials saved to .env", flush=True)
                return 0
            time.sleep(1)
    print("LOGIN_TIMEOUT", flush=True)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="WeChat iLink QR login")
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    parser.add_argument("--qr", type=Path, default=ROOT / "data/channel/wechat_login_qr.png")
    parser.add_argument("--timeout", type=int, default=480)
    args = parser.parse_args()
    try:
        return login(args.env, args.qr, args.timeout)
    except (httpx.HTTPError, ValueError, RuntimeError) as exc:
        # Request exceptions may contain the QR token or credentials.
        print(f"LOGIN_ERROR {type(exc).__name__}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

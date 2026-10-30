# -*- coding: utf-8 -*-
"""
refresh_state.py —— 云端 refreshToken 轮换状态持久化工具（artifact 链方案）。

背景：领克 refreshToken 是轮换制（每次续期发新值，旧值短时作废）。GitHub
Secrets 是静态的，云端每次续期拿到的新值随 runner 销毁而丢失，几天后
Secrets 里的旧值失效，任务开始持续报 user_refresh_invalid_expired。

方案：workflow 每次运行结束时把 env.json 中所有账号的最新 refreshToken 用
nativeAppSecret 派生密钥 AES-GCM 加密后更新为仓库内密文文件
LynkCoHelper/.rt-state.enc（公开可见但无害）；下次运行开始时解密恢复，
由 bootstrap_env.py 写回到组装出的 env.json 对应账号。Secrets 里的值仅作
冷启动种子。

多账号（v2）：persist 加密的是 JSON 映射
    {"accounts": [{"name": "主号", "refreshToken": "...", "deviceId": "..."}, ...]}
取自 env.json 的 users 数组；users 为空时回退单 user 节（name 取 "主号"）。
旧版单账号密文解密后是一段裸 refreshToken 字符串，bootstrap_env.py 按此
区分格式并兼容应用。

用法（在仓库根目录）：
    python LynkCoHelper/tools/refresh_state.py restore <密文文件>     # 解密 → stdout
    python LynkCoHelper/tools/refresh_state.py persist <env.json路径> <输出密文文件>
    python LynkCoHelper/tools/refresh_state.py selftest

密钥派生：sha256("rt-state:" + nativeAppSecret)。nativeAppSecret 来自
LYNKCO_APP_SECRETS JSON 或 env.json secrets.nativeAppSecret（复用 lynkco_common
的读取优先级）。密文格式：base64(nonce[12] + ciphertext + tag[16])。
"""
import base64
import hashlib
import json
import os
import sys

from Crypto.Cipher import AES

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from lynkco_common import env_value, load_env_data  # noqa: E402


def _derive_key() -> bytes:
    """从 nativeAppSecret 派生 32 字节 AES 密钥。"""
    secrets = load_env_data().get("secrets", {})
    app_secret = secrets.get("nativeAppSecret") or ""
    if not app_secret:
        raw = env_value("LYNKCO_APP_SECRETS") or ""
        if raw.strip():
            try:
                app_secret = json.loads(raw).get("nativeAppSecret") or ""
            except Exception:
                app_secret = ""
    if not app_secret:
        raise SystemExit("[refresh_state] 缺少 nativeAppSecret，无法派生密钥")
    return hashlib.sha256(("rt-state:" + app_secret).encode("utf-8")).digest()


def _encrypt(plaintext: str) -> str:
    # 显式 12 字节随机 nonce（pycryptodome 默认 16 字节，写死长度保证与解密端切分一致）
    cipher = AES.new(_derive_key(), AES.MODE_GCM, nonce=os.urandom(12))
    ct, tag = cipher.encrypt_and_digest(plaintext.encode("utf-8"))
    return base64.b64encode(cipher.nonce + ct + tag).decode("ascii")


def _decrypt(blob: str) -> str:
    raw = base64.b64decode(blob.strip())
    nonce, ct_tag = raw[:12], raw[12:]
    cipher = AES.new(_derive_key(), AES.MODE_GCM, nonce=nonce)
    pt = cipher.decrypt_and_verify(ct_tag[:-16], ct_tag[-16:])
    return pt.decode("utf-8")


def _extract_accounts(env_path: str) -> list:
    """从 env.json 提取账号凭据映射：users 数组优先，回退单 user 节。"""
    with open(env_path, encoding="utf-8") as f:
        data = json.load(f)
    raw_users = data.get("users")
    entries = raw_users if isinstance(raw_users, list) and raw_users else [
        data.get("user") or {}
    ]
    accounts = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        account = {
            "name": entry.get("name") or f"账号{index + 1}",
            "refreshToken": entry.get("refreshToken") or "",
            "deviceId": entry.get("deviceId") or "",
        }
        if account["refreshToken"]:
            accounts.append(account)
    return accounts


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)

    cmd = sys.argv[1]
    if cmd == "restore":
        if len(sys.argv) < 3:
            raise SystemExit("用法: refresh_state.py restore <密文文件>")
        with open(sys.argv[2], "r", encoding="ascii") as f:
            blob = f.read()
        sys.stdout.write(_decrypt(blob))

    elif cmd == "persist":
        if len(sys.argv) < 4:
            raise SystemExit("用法: refresh_state.py persist <env.json路径> <输出密文文件> [已有密文文件]")
        env_path, out_path = sys.argv[2], sys.argv[3]
        existing_path = sys.argv[4] if len(sys.argv) > 4 else None
        if not os.path.exists(env_path):
            raise SystemExit(f"[refresh_state] {env_path} 不存在，本次未轮换，跳过持久化")
        accounts = _extract_accounts(env_path)
        if not accounts:
            raise SystemExit("[refresh_state] env.json 中无 refreshToken，跳过持久化")

        # 密文每次 nonce 随机，无法按字节比较"是否轮换"；解密已有密文比对明文。
        if existing_path and os.path.exists(existing_path):
            try:
                with open(existing_path, "r", encoding="ascii") as f:
                    old_plain = _decrypt(f.read())
                try:
                    old_accounts = json.loads(old_plain).get("accounts") or []
                    old_keys = [(a.get("name"), a.get("refreshToken")) for a in old_accounts
                                if isinstance(a, dict)]
                except Exception:
                    # 旧版单账号格式：裸字符串，等价于"只有主号一个账号"
                    old_keys = [("主号", old_plain)] if old_plain else []
                new_keys = [(a["name"], a["refreshToken"]) for a in accounts]
                if old_keys == new_keys:
                    print("[refresh_state] 轮换值未变化，跳过持久化")
                    return
            except Exception:
                pass  # 解密失败（密钥变更等）→ 按有变化处理，写入新密文

        payload = json.dumps({"accounts": accounts}, ensure_ascii=False)
        with open(out_path, "w", encoding="ascii") as f:
            f.write(_encrypt(payload))
        print(f"[refresh_state] 已加密持久化 {len(accounts)} 个账号的 refreshToken → {out_path}")

    elif cmd == "selftest":
        # 本地自测：加密→解密 roundtrip（新 JSON 格式 + 旧裸字符串格式）
        token = "bearer-test-1234567890"
        assert _decrypt(_encrypt(token)) == token
        payload = json.dumps({"accounts": [{"name": "主号", "refreshToken": token, "deviceId": "d1"}]},
                             ensure_ascii=False)
        assert json.loads(_decrypt(_encrypt(payload)))["accounts"][0]["refreshToken"] == token
        print("[refresh_state] selftest OK")

    else:
        raise SystemExit(f"未知子命令: {cmd}")


if __name__ == "__main__":
    main()

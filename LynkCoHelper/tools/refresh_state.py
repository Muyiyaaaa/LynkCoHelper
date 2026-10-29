# -*- coding: utf-8 -*-
"""
refresh_state.py —— 云端 refreshToken 轮换状态持久化工具（artifact 链方案）。

背景：领克 refreshToken 是轮换制（每次续期发新值，旧值短时作废）。GitHub
Secrets 是静态的，云端每次续期拿到的新值随 runner 销毁而丢失，几天后
Secrets 里的旧值失效，任务开始持续报 user_refresh_invalid_expired。

方案：workflow 每次运行结束时把 env.json 中最新的 refreshToken 用
nativeAppSecret 派生密钥 AES-GCM 加密后上传为 artifact；下次运行开始时
下载解密恢复。Secrets 里的值仅作为冷启动种子。

用法（在仓库根目录）：
    python LynkCoHelper/tools/refresh_state.py restore <密文文件>     # 解密 → stdout
    python LynkCoHelper/tools/refresh_state.py persist <env.json路径> <输出密文文件>

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
            raise SystemExit("用法: refresh_state.py persist <env.json路径> <输出密文文件>")
        env_path, out_path = sys.argv[2], sys.argv[3]
        if not os.path.exists(env_path):
            raise SystemExit(f"[refresh_state] {env_path} 不存在，本次未轮换，跳过持久化")
        with open(env_path, encoding="utf-8") as f:
            rt = (json.load(f).get("user") or {}).get("refreshToken") or ""
        if not rt:
            raise SystemExit("[refresh_state] env.json 中无 refreshToken，跳过持久化")
        with open(out_path, "w", encoding="ascii") as f:
            f.write(_encrypt(rt))
        print(f"[refresh_state] 已加密持久化 refreshToken（{len(rt)} 字符）→ {out_path}")

    elif cmd == "selftest":
        # 本地自测：加密→解密 roundtrip
        token = "bearer-test-1234567890"
        assert _decrypt(_encrypt(token)) == token
        print("[refresh_state] selftest OK")

    else:
        raise SystemExit(f"未知子命令: {cmd}")


if __name__ == "__main__":
    main()

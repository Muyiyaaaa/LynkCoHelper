# -*- coding: utf-8 -*-
"""
bootstrap_env.py —— 云端运行前组装 env.json（多账号版）。

背景：env.json 含敏感凭据不进仓库，云端每次运行需从 Secrets + 上次轮换的
密文状态（refresh_state.py restore 的输出）组装出来。多账号（互点分享）
场景下 Secrets 静态存不了数组，用 LYNKCO_USERS_JSON（JSON 数组字符串）
提供各账号的冷启动凭据；refreshToken 轮换链仍由 .rt-state.enc 密文文件
承载，restore 出的映射按账号名回填覆盖。

来源优先级（refreshToken 字段）：
    轮换密文映射 > LYNKCO_USERS_JSON > 单账号 Secrets（LYNKCO_REFRESH_TOKEN）

用法（在仓库根目录）：
    LYNKCO_USERS_JSON='[{"name":"主号",...}]' \
    python LynkCoHelper/tools/bootstrap_env.py LynkCoHelper/env.json [.rt_restored]

成功后在 stdout 打印账号摘要（不打印任何凭据值）。
"""
import json
import os
import sys


def _load_users_from_secret() -> list:
    raw = (os.environ.get("LYNKCO_USERS_JSON") or "").strip()
    if not raw:
        return []
    users = json.loads(raw)
    if not isinstance(users, list):
        raise SystemExit("[bootstrap] LYNKCO_USERS_JSON 必须是 JSON 数组")
    return [u for u in users if isinstance(u, dict) and (u.get("refreshToken") or u.get("token"))]


def _load_users_from_single_secrets() -> list:
    rt = (os.environ.get("LYNKCO_REFRESH_TOKEN") or "").strip()
    if not rt:
        return []
    return [{
        "name": "主号",
        "refreshToken": rt,
        "deviceId": (os.environ.get("LYNKCO_DEVICE_ID") or "").strip(),
    }]


def _load_rotation_map(restored_path):
    """读取 restore 输出：新格式 JSON 映射，或旧版裸 refreshToken 字符串。"""
    if not restored_path or not os.path.exists(restored_path) or os.path.getsize(restored_path) == 0:
        return None, []
    with open(restored_path, encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        return None, []
    try:
        data = json.loads(content)
    except Exception:
        return content, []  # 旧版单账号格式：裸字符串
    accounts = data.get("accounts") if isinstance(data, dict) else None
    return None, [a for a in (accounts or []) if isinstance(a, dict) and a.get("refreshToken")]


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    out_path = sys.argv[1]
    restored_path = sys.argv[2] if len(sys.argv) > 2 else None

    users = _load_users_from_secret() or _load_users_from_single_secrets()
    if not users:
        raise SystemExit("[bootstrap] 缺少 LYNKCO_USERS_JSON / LYNKCO_REFRESH_TOKEN，无法组装 env.json")

    legacy_rt, rotated = _load_rotation_map(restored_path)

    # 轮换映射回填：按 name 精确匹配，匹配不上按下标兜底；旧版裸字符串只回填第一个账号。
    for index, user in enumerate(users):
        match = None
        if legacy_rt:
            match = {"refreshToken": legacy_rt} if index == 0 else None
        else:
            for rotated_account in rotated:
                if (rotated_account.get("name") or "").strip() == (user.get("name") or "").strip():
                    match = rotated_account
                    break
            if match is None and index < len(rotated):
                match = rotated[index]
        if match:
            user["refreshToken"] = match["refreshToken"]
            # deviceId 不轮换，但旧状态里可能比 Secrets 新，补缺不覆盖
            if match.get("deviceId") and not user.get("deviceId"):
                user["deviceId"] = match["deviceId"]

    app_secrets_raw = (os.environ.get("LYNKCO_APP_SECRETS") or "").strip()
    secrets = {}
    if app_secrets_raw:
        try:
            secrets = json.loads(app_secrets_raw)
        except Exception:
            raise SystemExit("[bootstrap] LYNKCO_APP_SECRETS 不是合法 JSON")

    env = {
        "user": users[0],
        "users": users if len(users) > 1 else [],
        "secrets": secrets,
        "notify": {"barkKey": (os.environ.get("LYNKCO_BARK_KEY") or "").strip()},
    }
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(env, f, ensure_ascii=False, indent=2)

    summary = ", ".join(
        f"{u.get('name') or f'账号{i + 1}'}(deviceId={'有' if u.get('deviceId') else '无'})"
        for i, u in enumerate(users)
    )
    source = "轮换密文" if (legacy_rt or rotated) else "Secrets 种子"
    print(f"[bootstrap] env.json 已组装：{len(users)} 个账号 [{summary}]，refreshToken 来源：{source}")


if __name__ == "__main__":
    main()

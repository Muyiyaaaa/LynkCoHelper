# -*- coding: utf-8 -*-
"""
Bark 推送通知工具模块，提供三个通用能力（不含任何业务逻辑，供
lynkco_daily_tasks.py 按需调用）：
    - build_title(result)：根据任务结果生成推送标题（含日期与积分变化）；
    - build_markdown_report(result)：把任务结果字典组装成 Bark Markdown 文案；
    - send_bark_notification(...)：把一段 Markdown 文案推送到 Bark。

配置方式：环境变量 LYNKCO_BARK_KEY，或 env.json 的 notify.barkKey 字段
（Bark App「我的」页面可查看），未配置时跳过推送并打印提示，不抛异常。
"""
import datetime

import requests

from lynkco_common import env_value, load_env_data

BARK_DEFAULT_BASE = "https://api.day.app"
BARK_CONNECT_TIMEOUT = 5
BARK_READ_TIMEOUT = 15

# 领克官方站点图标，供 Bark 服务端抓取后在通知里展示；抓取失败时
# Bark 会回退为 App 默认图标，不影响推送送达。
BARK_ICON_LYNKCO = "https://www.lynkco.com.cn/favicon.ico"


def _extract_point(energy_resp: dict) -> str:
    """从 myEnergy 响应中安全地取出 point 字段，取不到时返回 '?'。"""
    return str((energy_resp.get("data") or {}).get("point", "?"))


def _extract_income(energy_resp: dict) -> str:
    """从 myEnergy 响应中安全地取出 incomePoint（累计获得）字段。"""
    return str((energy_resp.get("data") or {}).get("incomePoint", "?"))


def _sign_state(result: dict) -> tuple:
    """
    汇总签到/分享的整体成败，返回 (emoji, 状态词)。签到失败优先级最高，
    其次分享失败；全部正常时返回 ✅。
    """
    if result.get("already_signed"):
        sign_ok = True
    else:
        sign_ok = bool((result.get("sign_result") or {}).get("success"))
    share_result = result.get("share_result")
    share_ok = share_result is None or bool(share_result.get("ok"))
    if sign_ok and share_ok:
        return "✅", "全部完成"
    if not sign_ok:
        return "❌", "签到失败"
    return "⚠️", "分享失败"


def build_title(result: dict) -> str:
    """生成推送标题：领克日报 · 10-14 · 今日+5（失败时显示状态词）。"""
    emoji, state = _sign_state(result)
    today = datetime.date.today().strftime("%m-%d")
    point_before = _extract_point(result.get("energy_before") or {})
    point_after = _extract_point(result.get("energy_after") or {})
    try:
        delta = int(point_after) - int(point_before)
        tail = f" · 今日+{delta}" if delta > 0 else (f" · 今日{delta}" if delta < 0 else "")
    except (ValueError, TypeError):
        tail = ""
    if emoji == "✅":
        return f"领克日报 · {today}{tail}"
    return f"{emoji} 领克日报 · {today} · {state}"


def _extract_expire(energy_resp: dict) -> int:
    """从 myEnergy 响应中取出即将过期的积分数量，取不到时返回 0。"""
    try:
        return int((energy_resp.get("data") or {}).get("expirePoint") or 0)
    except (ValueError, TypeError):
        return 0


def _energy_bar(grade: dict, width: int = 10) -> str:
    """
    根据能量体等级信息生成升级进度条：▰▰▱▱▱ 22% · 距Lv.2还需1942。
    进度 = 当前余额 / (当前余额 + 距下一级差额)。
    """
    try:
        cur = int(grade.get("energyNum"))
        need = int(grade.get("nextEnergyNum"))
        level = int(grade.get("energyLevel"))
    except (ValueError, TypeError):
        return ""
    if cur < 0 or need < 0:
        return ""
    total = cur + need
    filled = round(cur / total * width) if total > 0 else 0
    filled = max(0, min(width, filled))
    pct = round(cur / total * 100) if total > 0 else 0
    bar = "▰" * filled + "▱" * (width - filled)
    return f"{bar} {pct}% · 距 Lv.{level + 1} 还需 **{need}**"


def build_markdown_report(result: dict) -> str:
    """
    把 lynkco_daily_tasks.run_daily_tasks() 返回的结果字典组装成一段
    Bark markdown 推送内容（三段式：资产区 / 任务区 / 提醒引用块）。
    """
    assets = []
    tasks = []
    minor = []

    # ============ 资产区 ============
    # --- Co积分 ---
    point_before = _extract_point(result.get("energy_before") or {})
    point_after = _extract_point(result.get("energy_after") or {})
    income = _extract_income(result.get("energy_after") or {})
    try:
        delta = int(point_after) - int(point_before)
        delta_str = f"　今日 **+{delta}**" if delta > 0 else (f"　今日 **{delta}**" if delta < 0 else "")
    except (ValueError, TypeError):
        delta_str = ""
    assets.append(f"💰 **Co积分** **{point_after}**{delta_str}")

    # --- 能量体 ---
    grade = ((result.get("energy_grade") or {}).get("data") or {})
    energy_num = grade.get("energyNum")
    if energy_num is not None:
        level = grade.get("energyLevel")
        assets.append(f"⚡ **能量体** **{energy_num}**　Lv.{level}")
        bar = _energy_bar(grade)
        if bar:
            assets.append(bar)

    # ============ 任务区 ============
    # --- 签到 ---
    continue_data = (result.get("continue_info") or {}).get("data") or {}
    continue_days = continue_data.get("continueDays")
    sign_card = continue_data.get("signCardNumber")
    streak = f"已连续 {continue_days} 天" if continue_days is not None else ""

    if result.get("already_signed"):
        tasks.append(f"✅ **签到** {streak}".rstrip())
    else:
        sign_result = result.get("sign_result") or {}
        if bool(sign_result.get("success")):
            reward = (sign_result.get("data") or {}).get("rewardEnergyNumber")
            extra = f" · 奖励 **+{reward}**" if reward is not None else ""
            tasks.append(f"✅ **签到成功** {streak}{extra}".rstrip())
        else:
            tasks.append(f"❌ **签到失败** {sign_result.get('message', '未知错误')}")

    # --- 分享 ---
    share_result = result.get("share_result")
    share_title = ""
    if share_result is not None:
        if share_result.get("ok"):
            share_title = share_result.get("articleTitle") or ""
            tasks.append("🔗 **分享** 上报成功")
        else:
            detail = share_result.get("detail") or {}
            tasks.append(f"🔗 **分享** 失败（{detail.get('message', '详情见日志')}）")

    # ============ 提醒引用块 ============
    if share_title:
        minor.append(f"📰 《{share_title}》")
    expire = _extract_expire(result.get("energy_after") or {})
    if expire > 0:
        minor.append(f"⏰ {expire} 枚 Co积分即将过期，记得消耗")
    if sign_card is not None:
        minor.append(f"🎫 签到卡 {sign_card} 张")
    if income not in ("?", ""):
        minor.append(f"💼 Co积分累计 {income}")

    body = "\n".join(assets + [""] + tasks)
    if minor:
        body += "\n\n> " + "\n> ".join(minor)
    return body


def send_bark_notification(title: str, markdown_body: str, group: str = "LynkCo签到",
                            icon: str = None, level: str = "timeSensitive",
                            bark_key: str = None, open_url: str = None) -> dict:
    """
    通过 Bark 发送一条 Markdown 格式的推送通知。level 可选
    "critical"/"active"/"timeSensitive"/"passive"，默认 timeSensitive
    （允许在专注模式下弹出）。bark_key 不传则读取环境变量
    LYNKCO_BARK_KEY，未配置时返回 {"skipped": True} 且不抛异常。
    """
    bark_key = bark_key or env_value("LYNKCO_BARK_KEY") or load_env_data().get("notify", {}).get("barkKey", "").strip()
    if not bark_key:
        print("[提示] 未配置 LYNKCO_BARK_KEY，跳过 Bark 推送。")
        return {"skipped": True}

    url = f"{BARK_DEFAULT_BASE}/{bark_key}"

    payload = {
        "title": title,
        "markdown": markdown_body,
        "group": group,
        "level": level,
    }
    if icon:
        payload["icon"] = icon
    if open_url:
        payload["url"] = open_url

    # requests may inherit a desktop-wide proxy that can connect but never
    # return Bark's response. Bark is directly reachable, so use a clean
    # session and separate short connect/read timeouts for this notification.
    session = requests.Session()
    session.trust_env = False
    resp = session.post(
        url, json=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=(BARK_CONNECT_TIMEOUT, BARK_READ_TIMEOUT),
    )
    resp.raise_for_status()
    return resp.json()

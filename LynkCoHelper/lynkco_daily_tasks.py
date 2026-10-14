# -*- coding: utf-8 -*-
"""
领克App 每日任务编排入口（签到 + 分享 + 积分查询 + 结果通知）。

- run_daily_tasks()：编排"查积分 -> 签到 -> 分享 -> 再查积分对比"，返回结构化结果字典。
- run_and_notify()：加载 token -> 执行每日任务 -> 组装 Markdown -> 推送到 Bark。

签到/分享成功后 myEnergy 接口的积分有几秒异步延迟，故查询"之后积分"前会先 sleep。

用法:
    python3 lynkco_daily_tasks.py    # 执行每日任务（签到+分享）并推送结果
"""
import json
import os
import sys
import time

from lynkco_common import env_value, mask_sensitive
from lynkco_login import load_token
from lynkco_notify import build_markdown_report, build_title, send_bark_notification
from lynkco_sign import LynkCoSignClient
from lynkco_share import LynkCoShareClient

# 签到/分享完成后，等待多久再查询"之后积分"，单位秒。经真机验证 3~5 秒足够
# 让服务端把能量体变化同步到 myEnergy 接口，可通过环境变量覆盖。
ENERGY_REFRESH_DELAY_SECONDS = float(env_value("LYNKCO_ENERGY_DELAY", "5"))

EP_MY_ENERGY = "/app/energy/myEnergy"
# 能量体等级信息（余额 energyNum/等级 energyLevel/下一级门槛 nextEnergyNum），
# 经真机抓包验证该路径可直接走 app-api-gw-toc 网关 + 原生签名调用。
EP_ENERGY_GRADE = "/app/user/privilegePackage/energyGradeInfo"
# 积分/能量体规则页 H5 地址（预留，暂未使用）。
EP_RULE_LIST = "/app/rule/list"


def get_my_energy(client: LynkCoSignClient) -> dict:
    """查询当前账号的 Co积分（GET /app/energy/myEnergy，与签到共用同一网关/签名体系）。"""
    resp = client._request("GET", EP_MY_ENERGY)
    return resp.json()


def get_energy_grade(client: LynkCoSignClient) -> dict:
    """查询当前账号的能量体余额与等级（GET /app/user/privilegePackage/energyGradeInfo）。"""
    resp = client._request("GET", EP_ENERGY_GRADE)
    return resp.json()


def get_rule_urls(client: LynkCoSignClient) -> dict:
    """查询积分/能量体规则页地址（GET /app/rule/list），失败时返回空字典。"""
    resp = client._request("GET", EP_RULE_LIST)
    return (resp.json().get("data") or {})


def run_daily_tasks(token: str, do_share: bool = True) -> dict:
    """
    编排执行一次完整的"签到 + 分享"流程，返回汇总结果字典：
    {"energy_before", "day_info", "already_signed", "sign_result", "continue_info",
     "share_result", "energy_after"}（已签到/do_share=False 时对应字段为 None）。

    do_share 默认 True，供需要在测试/脚本中跳过分享的调用方使用；
    正式入口 run_and_notify() 始终以 do_share=True 调用。
    """
    result = {}

    sign_client = LynkCoSignClient(token)

    result["energy_before"] = get_my_energy(sign_client)

    day_info = sign_client.get_sign_day_info()
    result["day_info"] = day_info

    already_signed = (day_info.get("data") or {}).get("signStatus") == 1
    result["already_signed"] = already_signed

    if already_signed:
        result["sign_result"] = None
    else:
        result["sign_result"] = sign_client.do_sign()

    result["continue_info"] = sign_client.get_continue_days()

    if do_share:
        share_client = LynkCoShareClient(token)
        try:
            result["share_result"] = share_client.do_share()
        except Exception as e:
            result["share_result"] = {"ok": False, "detail": {"message": f"分享任务异常: {e}"}}
    else:
        result["share_result"] = None

    # 积分变化有异步延迟，等待片刻再查询，避免看到"没有变化"的假象。
    if not already_signed or (do_share and (result.get("share_result") or {}).get("ok")):
        time.sleep(ENERGY_REFRESH_DELAY_SECONDS)
    result["energy_after"] = get_my_energy(sign_client)

    # 能量体余额与等级（失败不影响主任务，置 None 后推送端自动省略）
    try:
        result["energy_grade"] = get_energy_grade(sign_client)
    except Exception as e:
        print(f"[警告] 能量体等级查询失败（不影响其他任务）: {e}")
        result["energy_grade"] = None

    # 规则页地址（供推送点击跳转，失败置空）
    try:
        result["rule_urls"] = get_rule_urls(sign_client)
    except Exception:
        result["rule_urls"] = {}

    return result


def run_and_notify() -> dict:
    """
    完整入口：加载 token -> 执行每日任务(run_daily_tasks) -> 组装 Markdown
    -> 推送到 Bark（lynkco_notify 模块）。

    每日任务固定包含"签到+分享"两项（分享接口每日有加分次数上限，重复
    调用不会重复加分，详见 docs/分享任务接口说明.md）。

    返回值同 run_daily_tasks()，并额外附带 "notify_result" 字段（Bark
    推送接口的响应，或 {"skipped": True}）。
    """
    token = load_token()

    print("=== 执行每日任务（签到+分享）===")
    result = run_daily_tasks(token, do_share=True)
    print(json.dumps(mask_sensitive(result), ensure_ascii=False, indent=2))

    markdown_body = build_markdown_report(result)
    print("\n=== 推送内容预览 ===")
    print(markdown_body)

    icon = env_value("LYNKCO_BARK_ICON") or None
    try:
        notify_result = send_bark_notification(
            title=build_title(result),
            markdown_body=markdown_body,
            icon=icon,
        )
    except Exception as e:
        # 推送失败（网络问题/代理超时等）不应影响签到/分享本身已经成功执行
        # 这一事实，只记录警告，不让整个流程以异常状态退出。
        print(f"[警告] Bark 推送失败（不影响签到/分享结果）: {e}")
        notify_result = {"skipped": True, "error": str(e)}
    print("\n=== Bark 推送结果 ===")
    print(json.dumps(mask_sensitive(notify_result), ensure_ascii=False, indent=2))

    result["notify_result"] = notify_result
    return result


def main():
    try:
        run_and_notify()
    except RuntimeError as e:
        print(f"[错误] {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[错误] 执行失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

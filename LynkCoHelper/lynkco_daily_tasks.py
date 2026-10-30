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
# 积分任务清单（真机抓包确认）：返回"日常任务/进阶任务"两目录，共 22 项任务，
# 含 taskName/pointExplain（奖励说明）/taskExplain（完成条件）/taskStatus/enable。
EP_POINT_TASKS = "/up/api/v1/user/collection/detail/point"
# 积分过期提醒（真机抓包确认）：{"expriedDate": "...", "point": N}。
EP_ENERGY_EXPIRE = "/app/energy/expire"


def _as_dict(value) -> dict:
    """接口对失效 token 可能返回 JSON 字符串而非对象，统一守卫为 dict。"""
    return value if isinstance(value, dict) else {}


def get_my_energy(client: LynkCoSignClient) -> dict:
    """查询当前账号的 Co积分（GET /app/energy/myEnergy，与签到共用同一网关/签名体系）。"""
    resp = client._request("GET", EP_MY_ENERGY)
    return _as_dict(resp.json())


def get_energy_grade(client: LynkCoSignClient) -> dict:
    """查询当前账号的能量体余额与等级（GET /app/user/privilegePackage/energyGradeInfo）。"""
    resp = client._request("GET", EP_ENERGY_GRADE)
    return _as_dict(resp.json())


def get_rule_urls(client: LynkCoSignClient) -> dict:
    """查询积分/能量体规则页地址（GET /app/rule/list），失败时返回空字典。"""
    resp = client._request("GET", EP_RULE_LIST)
    return _as_dict((resp.json() or {}).get("data"))


def get_point_tasks(client: LynkCoSignClient) -> dict:
    """查询积分任务清单（GET /up/api/v1/user/collection/detail/point），失败返回空字典。"""
    resp = client._request("GET", EP_POINT_TASKS)
    return _as_dict((resp.json() or {}).get("data"))


def get_energy_expire(client: LynkCoSignClient) -> dict:
    """查询即将过期的积分（GET /app/energy/expire），失败返回空字典。"""
    resp = client._request("GET", EP_ENERGY_EXPIRE)
    return _as_dict((resp.json() or {}).get("data"))


def summarize_point_tasks(data: dict) -> list:
    """把任务清单整理成扁平行列表 [{"catalogue","taskId","name","reward","rule","enabled"}]。"""
    rows = []
    for catalogue in data.get("catalogueList") or []:
        catalogue_name = catalogue.get("catalogueName") or ""
        for task in catalogue.get("taskList") or []:
            rows.append({
                "catalogue": catalogue_name,
                "taskId": task.get("taskId"),
                "name": task.get("taskName") or "",
                "reward": task.get("pointExplain") or "",
                "rule": task.get("taskExplain") or "",
                "enabled": task.get("enable") not in (0, False),
            })
    return rows


def run_daily_tasks(token: str, do_share: bool = True, do_cross_share: bool = True) -> dict:
    """
    编排执行一次完整的"签到 + 分享（+ 多账号互点）"流程，返回汇总结果字典：
    {"energy_before", "day_info", "already_signed", "sign_result", "continue_info",
     "share_result", "cross_share", "milestones", "energy_after"}
    （已签到/do_share=False/do_cross_share 跳过时对应字段为 None）。

    do_share 默认 True，供需要在测试/脚本中跳过分享的调用方使用；
    正式入口 run_and_notify() 始终以 do_share=True 调用。
    do_cross_share=True 时若 env.json 配置了 ≥2 个账号（users 数组），会执行
    双账号互点分享（每号每天额外一次"被点击"加分机会）。
    """
    result = {}

    sign_client = LynkCoSignClient(token)

    result["energy_before"] = get_my_energy(sign_client)

    day_info = _as_dict(sign_client.get_sign_day_info())
    result["day_info"] = day_info

    already_signed = (day_info.get("data") or {}).get("signStatus") == 1
    result["already_signed"] = already_signed

    if already_signed:
        result["sign_result"] = None
    else:
        result["sign_result"] = _as_dict(sign_client.do_sign())

    continue_info = _as_dict(sign_client.get_continue_days())
    result["continue_info"] = continue_info

    # 签到里程碑进度（7/30/85/365 天），仅整理展示，不额外发请求。
    try:
        result["milestones"] = sign_client.get_milestone_status(continue_info)
    except Exception as e:
        print(f"[警告] 里程碑进度整理失败（不影响其他任务）: {e}")
        result["milestones"] = None

    if do_share:
        share_client = LynkCoShareClient(token)
        try:
            result["share_result"] = share_client.do_share()
        except Exception as e:
            result["share_result"] = {"ok": False, "detail": {"message": f"分享任务异常: {e}"}}
    else:
        result["share_result"] = None

    # 多账号互点分享：主账号签到+自分享完成后执行，为两个账号各争取一次
    # "被点击"加分（服务端有每日上限，未配置第二账号时自动跳过）。
    result["cross_share"] = None
    if do_share and do_cross_share:
        try:
            from lynkco_login import load_accounts
            from lynkco_share import run_cross_share_for_accounts
            accounts = load_accounts()
            if len(accounts) >= 2:
                result["cross_share"] = run_cross_share_for_accounts(accounts)
        except Exception as e:
            print(f"[警告] 互点分享执行失败（不影响主任务）: {e}")
            result["cross_share"] = {"ok": False, "detail": {"message": str(e)}}

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

    # 积分任务清单（官方 22 项：分享热爱/评论加精/投稿收录等），失败不影响主任务
    try:
        result["point_tasks"] = summarize_point_tasks(get_point_tasks(sign_client))
    except Exception as e:
        print(f"[警告] 积分任务清单查询失败（不影响其他任务）: {e}")
        result["point_tasks"] = []

    # 积分过期提醒，失败置空
    try:
        result["energy_expire"] = get_energy_expire(sign_client)
    except Exception:
        result["energy_expire"] = {}

    # 统一类型守卫：失效 token 下部分接口返回 JSON 字符串，组装前规范为 dict，
    # 避免推送渲染阶段出现 'str' object has no attribute 'get'
    for key in ("energy_before", "energy_after", "day_info", "sign_result",
                "continue_info", "energy_grade", "rule_urls", "energy_expire"):
        result[key] = _as_dict(result.get(key))

    return result


def run_and_notify() -> dict:
    """
    完整入口：加载所有账号 -> 逐账号执行每日任务(run_daily_tasks：签到+分享+积分查询)
    -> 配置 ≥2 个账号时执行一次互点分享 -> 逐账号组装 Markdown -> 推送到 Bark。

    多账号时串行执行（非并发），每个账号独立推送一条（标题带账号名区分）；
    互点分享结果并入第一个账号的推送。单账号行为与旧版完全一致。

    返回第一个账号的任务结果字典（含 notify_result），全部结果在 "all_results"。
    """
    from lynkco_login import load_accounts, load_token_for

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    accounts = load_accounts()
    results = []
    for idx, account in enumerate(accounts):
        label = account.get("name") or f"账号{idx + 1}"
        try:
            token = load_token_for(account, label=label)
        except RuntimeError as e:
            print(f"[错误] [{label}] {e}，跳过该账号。")
            results.append({"account": label, "error": str(e)})
            continue
        print(f"\n=== [{label}] 执行每日任务（签到+分享）===")
        result = run_daily_tasks(token, do_share=True, do_cross_share=False)
        result["account"] = label
        print(json.dumps(mask_sensitive(result), ensure_ascii=False, indent=2))
        results.append(result)

    # 互点分享：所有账号签到+分享完成后执行一次（A/B 互为访客，双向各 +5 机会）
    cross_result = None
    if len(results) >= 2 and all("error" not in r for r in results[:2]):
        try:
            from lynkco_share import run_cross_share_for_accounts
            cross_result = run_cross_share_for_accounts(accounts)
        except Exception as e:
            print(f"[警告] 互点分享执行失败（不影响各账号主任务）: {e}")
            cross_result = {"ok": False, "detail": {"message": str(e)}}
        if results:
            results[0]["cross_share"] = cross_result

    # 逐账号推送
    icon = env_value("LYNKCO_BARK_ICON") or None
    multi = len(results) > 1
    for result in results:
        if "error" in result:
            try:
                send_bark_notification(
                    title=f"{'❌ ' if multi else ''}领克日报 · {result['account']}",
                    markdown_body=f"登录态失效：{result['error']}",
                    group="LynkCo签到", icon=icon,
                )
            except Exception as e:
                print(f"[警告] [{result['account']}] 失败通知推送失败: {e}")
            continue
        markdown_body = build_markdown_report(result)
        print(f"\n=== [{result.get('account')}] 推送内容预览 ===")
        print(markdown_body)
        title = build_title(result)
        if multi:
            title = f"{result.get('account')} · {title}"
        try:
            notify_result = send_bark_notification(
                title=title, markdown_body=markdown_body, icon=icon,
            )
        except Exception as e:
            print(f"[警告] Bark 推送失败（不影响签到/分享结果）: {e}")
            notify_result = {"skipped": True, "error": str(e)}
        print("\n=== Bark 推送结果 ===")
        print(json.dumps(mask_sensitive(notify_result), ensure_ascii=False, indent=2))
        result["notify_result"] = notify_result

    first = results[0] if results else {}
    first["all_results"] = results
    return first


def print_point_tasks() -> None:
    """CLI：打印当前账号的积分任务清单（官方 22 项，含奖励与完成条件）。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    token = load_token()
    client = LynkCoSignClient(token)
    data = get_point_tasks(client)
    rows = summarize_point_tasks(data)
    if not rows:
        print("[提示] 未取到任务清单（接口可能需登录态或临时不可用）。")
        return
    print(f"\n=== 积分任务清单：{data.get('collectionName') or '积分'} / "
          f"共 {len(rows)} 项（{data.get('catalogueNum')} 个目录）===\n")
    current_cat = None
    for row in rows:
        if row["catalogue"] != current_cat:
            current_cat = row["catalogue"]
            print(f"[{current_cat}]")
        flag = "" if row["enabled"] else "（当前未开放）"
        print(f"  {str(row['taskId']):<6} {row['reward']:<18} {row['name']}{flag}")
        if row["rule"]:
            print(f"         ↳ {row['rule']}")
    print()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="领克 App 每日任务：签到 + 分享 + 积分查询")
    parser.add_argument("--tasks", action="store_true", help="仅打印积分任务清单（官方 22 项）")
    parser.add_argument("--like", action="store_true", help="仅给探索广场最新文章点赞一次")
    args = parser.parse_args()

    if args.tasks:
        try:
            print_point_tasks()
        except Exception as e:
            print(f"[错误] 查询任务清单失败: {e}")
            sys.exit(1)
        return

    if args.like:
        try:
            token = load_token()
            from lynkco_share import LynkCoShareClient, like_latest_article
            result = like_latest_article(LynkCoShareClient(token))
            print(json.dumps(mask_sensitive(result), ensure_ascii=False, indent=2))
        except Exception as e:
            print(f"[错误] 点赞失败: {e}")
            sys.exit(1)
        return

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

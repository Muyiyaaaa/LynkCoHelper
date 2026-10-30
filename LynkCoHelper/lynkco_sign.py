# -*- coding: utf-8 -*-
"""
领克App 每日签到脚本。签名算法详见 lynkco_common.py / docs/AppSecret_逆向分析记录.md。

使用前提：需要有效 token，由 lynkco_login.py 的 load_token() 统一提供
（自动续期或人工获取），本脚本无需关心 token 具体来源。

2026-07 抓包更新：真正"执行签到"的接口路径已从 /up/api/v1/user/sign 变为
/up/api/v1/user/sign/upgrade，统一使用最新 HAR 的 iOS 原生签名头；POST body
即使是空对象 "{}" 也要计算 Content-MD5。积分、签到状态和连续签到查询也使用同一
签名体系；daily task 不再依赖 H5 签名密钥。
"""
import json
import sys

import requests

from lynkco_common import (
    BASE_URL,
    build_ios_signature,
    build_native_app_headers,
    env_value,
    get_extra_endpoint,
    mask_sensitive,
    request_with_retry,
)
from lynkco_login import load_token

EP_SIGN_DAY_INFO = "/up/api/v1/user/sign/day/info"
EP_SIGN_UPGRADE = "/up/api/v1/user/sign/upgrade"  # 真正执行签到的接口（抓包确认，非 /up/api/v1/user/sign）
EP_CONTINUE_DAYS = "/up/api/v1/userReward/getContinueDaysAndSignCard"

# 以下写操作端点尚未抓包确认，不硬编码猜测路径；由 env.json["extraEndpoints"] 或
# 同名环境变量 LYNKCO_EP_* 注入后才启用（见 get_extra_endpoint()），未配置时安全跳过。
#   reward_receive : 签到里程碑奖励领取（连续 7/30/85/365 天的能量体/补签卡/Co积分）
#   sign_repair    : 使用签到卡补签
#   like_content   : 探索广场内容点赞（社区互动任务加分）


class LynkCoSignClient:
    def __init__(self, token: str, device_id: str = None):
        if not token:
            raise ValueError("token 不能为空")
        # 领克接口的 token 头需要 "bearer" 前缀且中间无空格，若外部传入时已带前缀则不重复添加
        self.token = token if token.startswith("bearer") else f"bearer{token}"
        # 多账号场景带发起账号自己的 deviceId，避免兜底到主号设备号造成 token/设备不一致
        self.device_id = (device_id or "").strip() or None
        self.session = requests.Session()

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """走已验证的 App 原生签名体系，用于积分和签到查询接口。"""
        extra_headers = kwargs.pop("extra_headers", {})
        url = BASE_URL + path
        resp = request_with_retry(
            self.session, method, url,
            build_headers=lambda: {
                **build_ios_signature(
                    method, path, token=self.token, query=kwargs.get("params"),
                    accept="application/json", content_type="application/json; charset=UTF-8",
                ),
                "ca_version": "1",
                "x-requiretoken": "false",
                **build_native_app_headers(device_id=self.device_id),
                **extra_headers,
            },
            **kwargs,
        )
        try:
            resp.json()
        except ValueError:
            raise RuntimeError(
                f"[{method} {path}] 接口未返回有效 JSON（可能被网关拦截，如境外 IP 访问限制"
                f"或 AppKey 未授权），HTTP {resp.status_code}，"
                f"响应体前200字符: {resp.text[:200]!r}"
            )
        return resp

    def _native_request(self, method: str, path: str, body: bytes = None, **kwargs) -> requests.Response:
        """走公共 iOS 原生签名体系，用于 sign/upgrade 等写操作接口。"""
        extra_headers = kwargs.pop("extra_headers", {})
        url = BASE_URL + path
        resp = request_with_retry(
            self.session, method, url,
            build_headers=lambda: {
                **build_ios_signature(
                    method, path, token=self.token,
                    accept="application/json", content_type="application/json; charset=UTF-8",
                    body=body,
                ),
                "ca_version": "1",
                "x-requiretoken": "false",
                **build_native_app_headers(device_id=self.device_id),
                **extra_headers,
            },
            data=body,
            **kwargs,
        )
        try:
            resp.json()
        except ValueError:
            raise RuntimeError(
                f"[Native {method} {path}] 接口未返回有效 JSON，HTTP {resp.status_code}，"
                f"响应体前200字符: {resp.text[:200]!r}"
            )
        return resp

    def get_sign_day_info(self) -> dict:
        """查询今日签到状态"""
        resp = self._request("GET", EP_SIGN_DAY_INFO)
        return resp.json()

    def do_sign(self) -> dict:
        """执行签到（POST /up/api/v1/user/sign/upgrade，原生签名，body 固定为空对象 "{}"）"""
        resp = self._native_request("POST", EP_SIGN_UPGRADE, body=b"{}")
        return resp.json()

    def get_continue_days(self) -> dict:
        """查询连续签到天数和签到卡数量"""
        resp = self._request("GET", EP_CONTINUE_DAYS)
        return resp.json()

    # ------------------------- 里程碑领取 / 补签（端点待抓包，可配置启用） -------------------------

    SIGN_MILESTONES = (7, 30, 85, 365)

    def _signed_native_post(self, path: str, params: dict = None, body: bytes = b"{}") -> requests.Response:
        """POST 请求，query 参数与 body 均参与 iOS 原生签名（_native_request 的签名不含 query）。"""
        url = BASE_URL + path
        return request_with_retry(
            self.session, "POST", url,
            build_headers=lambda: {
                **build_ios_signature(
                    "POST", path, token=self.token, query=params,
                    accept="application/json", content_type="application/json; charset=UTF-8",
                    body=body,
                ),
                "ca_version": "1",
                "x-requiretoken": "false",
                **build_native_app_headers(token=self.token),
            },
            params=params, data=body,
        )

    def get_milestone_status(self, continue_info: dict = None) -> dict:
        """
        根据连续签到信息整理里程碑进度（7/30/85/365 天），返回：
            {"continueDays": <当前连续天数>, "signCardNumber": <签到卡数>,
             "milestones": [{"days": 7, "achieved": true/false}, ...]}
        continue_info 不传则内部查询一次。该接口返回体中若直接携带任务进度字段，
        会原样透传到 "raw" 键，供人工核对（避免对未确认字段做硬解析）。
        """
        if continue_info is None:
            continue_info = self.get_continue_days()
        data = continue_info.get("data") or {}
        try:
            continue_days = int(data.get("continueDays") or 0)
        except (TypeError, ValueError):
            continue_days = 0
        return {
            "continueDays": continue_days,
            "signCardNumber": data.get("signCardNumber"),
            "milestones": [
                {"days": m, "achieved": continue_days >= m}
                for m in self.SIGN_MILESTONES
            ],
            "raw": data,
        }

    def receive_sign_reward(self, milestone_days: int = None) -> dict:
        """
        领取签到里程碑奖励。端点必须通过 env.json["extraEndpoints"]["rewardReceive"]
        或环境变量 LYNKCO_EP_REWARD_RECEIVE 配置（抓包确认后再启用），未配置时返回
        {"ok": False, "skipped": True, ...}，不发起任何请求。
        milestone_days 通过 query 参数 milestoneDays 传递（服务端字段名以抓包为准，
        可后续调整本方法的 body/query 构造）。
        """
        path = get_extra_endpoint("rewardReceive")
        if not path:
            print("[提示] 未配置 rewardReceive 端点（LYNKCO_EP_REWARD_RECEIVE / extraEndpoints），跳过里程碑奖励领取。")
            return {"ok": False, "skipped": True, "reason": "endpoint_not_configured"}
        params = {"milestoneDays": str(milestone_days)} if milestone_days else None
        resp = self._signed_native_post(path, params=params)
        result = resp.json()
        return {"ok": bool(result.get("success")), "milestone": milestone_days, "detail": result}

    def repair_sign(self, date_str: str) -> dict:
        """
        使用签到卡补签指定日期（YYYY-MM-DD）。端点必须通过
        env.json["extraEndpoints"]["signRepair"] 或 LYNKCO_EP_SIGN_REPAIR 配置，
        未配置时返回 {"ok": False, "skipped": True, ...}，不发起任何请求。
        date_str 经 query 参数 repairDate 传递（字段名以抓包为准）。
        """
        path = get_extra_endpoint("signRepair")
        if not path:
            print("[提示] 未配置 signRepair 端点（LYNKCO_EP_SIGN_REPAIR / extraEndpoints），跳过补签。")
            return {"ok": False, "skipped": True, "reason": "endpoint_not_configured"}
        resp = self._signed_native_post(path, params={"repairDate": date_str})
        result = resp.json()
        return {"ok": bool(result.get("success")), "date": date_str, "detail": result}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="领克App 每日签到 / 里程碑 / 补签")
    parser.add_argument("--milestones", action="store_true", help="查询签到里程碑进度并尝试领取已达标奖励（需配置 rewardReceive 端点）")
    parser.add_argument("--repair", metavar="YYYY-MM-DD", help="使用签到卡补签指定日期（需配置 signRepair 端点）")
    args = parser.parse_args()

    try:
        token = load_token()
    except RuntimeError as e:
        print(f"[错误] {e}")
        sys.exit(1)

    client = LynkCoSignClient(token)

    print("=== 查询签到状态 ===")
    try:
        day_info = client.get_sign_day_info()
        print(json.dumps(mask_sensitive(day_info), ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"[错误] 查询签到状态失败: {e}")
        sys.exit(1)

    if not day_info.get("success"):
        print("[错误] token 可能已失效，请重新登录获取新 token。")
        sys.exit(1)

    already_signed = day_info.get("data", {}).get("signStatus") == 1
    if already_signed:
        print("\n今日已签到，无需重复签到。")
    else:
        print("\n=== 执行签到 ===")
        try:
            sign_result = client.do_sign()
            print(json.dumps(mask_sensitive(sign_result), ensure_ascii=False, indent=2))
            if sign_result.get("success"):
                print("\n签到成功！")
            else:
                print(f"\n签到失败: {sign_result.get('message')}")
        except Exception as e:
            print(f"[错误] 签到请求失败: {e}")
            sys.exit(1)

    print("\n=== 连续签到信息 ===")
    continue_info = {}
    try:
        continue_info = client.get_continue_days()
        print(json.dumps(mask_sensitive(continue_info), ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"[警告] 查询连续签到信息失败: {e}")

    if args.milestones:
        print("\n=== 签到里程碑进度 ===")
        try:
            status = client.get_milestone_status(continue_info or None)
            for m in status["milestones"]:
                mark = "✅ 已达成" if m["achieved"] else "⬜ 未达成"
                print(f"  连续签到 {m['days']} 天：{mark}")
            for m in status["milestones"]:
                if m["achieved"]:
                    claim = client.receive_sign_reward(m["days"])
                    print(f"  领取 {m['days']} 天奖励: {json.dumps(mask_sensitive(claim), ensure_ascii=False)}")
        except Exception as e:
            print(f"[警告] 里程碑处理失败: {e}")

    if args.repair:
        print(f"\n=== 补签 {args.repair} ===")
        try:
            result = client.repair_sign(args.repair)
            print(json.dumps(mask_sensitive(result), ensure_ascii=False, indent=2))
        except Exception as e:
            print(f"[错误] 补签失败: {e}")

    # 分享任务已独立到 lynkco_share.py，每次运行本脚本都会执行一次（接口
    # 每日有加分次数上限，重复调用不会重复加分，详见 docs/分享任务接口说明.md）。
    from lynkco_share import run_auto_share
    run_auto_share(token)


if __name__ == "__main__":
    main()

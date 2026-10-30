# -*- coding: utf-8 -*-
"""
领克App 分享任务脚本。

分享流程使用 App 原生签名体系；分享落地页仍需携带 H5 Origin/Referer，但这不
代表使用 H5 签名。接口协议细节、已知限制见
docs/分享任务接口说明.md。重要提醒：接口返回 success 不代表真正加分（每日
有次数上限，重复调用不会重复加分），需自行对比 myEnergy 的 point 字段判断。

do_share() 已封装好"优先简化两步法，失败/无 content_id 时可选回退完整三步法"的
逻辑。通过 lynkco_sign.py / lynkco_daily_tasks.py 运行时会自动执行一次分享；
也可直接运行本文件单独触发一次分享。
"""
import base64
import hashlib
import hmac
import json
import sys
import time
import uuid

import requests

import lynkco_common
from lynkco_common import (
    APP_VERSION,
    BASE_URL,
    build_ios_security_info,
    build_ios_signature,
    build_native_app_headers,
    env_value,
    get_extra_endpoint,
    mask_sensitive,
    request_with_retry,
)
from lynkco_login import load_token

# ------------------------- 分享任务相关端点，完整协议见 docs/分享任务接口说明.md -------------------------
EP_GET_SHARE_CODE = "/app/v1/task/getShareCode"          # 获取本次分享的一次性 shareCode（原生签名）
EP_SHARE_LOOKUP = "/app/v1/task/shareCodeToUserId"        # 通过 shareCode 反查分享人 userId
EP_SHARE_CHECK = "/app/v1/task/shareContentContectCheck"  # 分享前置校验，可选步骤
EP_SHARE_REPORT = "/app/v1/task/shareContentContectReporting"  # 完整版上报，另一账号点开链接后走的三步流程
# 简化版上报：拿到 getShareCode 返回的 shareCode 后，POST 到该接口即可让服务端
# 记为"已完成一次分享"，比 lookup+check+report 三步法更直接，单账号即可自己触发。
EP_SHARE_REPORTING_SIMPLE = "/app/v1/task/shareReporting"
# 探索广场首页文章流，用于动态取最新文章 id，避免固定文章失效。body 需带
# dynamicSort/uniqueId/refreshType/pageNo 分页参数；否则只返回首屏、命中"文章"
# 类型的概率很低。
EP_EXPLORE_SQUARE_INDEX = "/app/explore/home-page/square/index2"
EP_EXPLORE_HOME_V3_LIST = "/app/explore/home-page/v3/list"
# get_latest_article() 命中第一篇文章前最多尝试翻的页数。
EXPLORE_SQUARE_PAGE_COUNT = 5

# 分享落地页 H5 域名（与签到用的 h5.lynkco.cn 是两个不同域名/Origin，
# shareReporting 接口的签名 AppKey 虽然相同，但网关会校验 Origin/Referer）。
H5_SHARE_ORIGIN = "https://h5.lynkco.com"

# 典型文章 id（真实抓包样本），作为 get_latest_article() 获取失败时的兜底。
DEFAULT_SHARE_ARTICLE_ID = "2075054309774663680"


def article_share_url(article_id: str) -> str:
    """Return the article landing URL used by the share task."""
    return (
        "https://h5.lynkco.com/app-h5/dist/web/pages/exploration/article/index.html"
        f"?id={article_id}&isShare=lynkco%3A%2F%2Fwx%2F%3FrouteUrl%3D%2Fpages%2Fexploration%2Farticle%2Findex.js%3Fid%3D{article_id}"
    )


def _find_article(value) -> dict:
    """递归查找广场文章流中第一个“文章”类型内容，返回 {"articleId", "title"}，未找到则返回空 dict。"""
    if isinstance(value, dict):
        article_id = value.get("articleId")
        content_type = value.get("contentType") or value.get("contentTypeCode")
        if article_id and (not content_type or content_type in ("文章", "article")):
            return {"articleId": str(article_id), "title": str(value.get("title") or "")}
        for child in value.values():
            found = _find_article(child)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_article(item)
            if found:
                return found
    return {}


class LynkCoShareClient:
    def __init__(self, token: str, device_id: str = None):
        if not token:
            raise ValueError("token 不能为空")
        self.token = token if token.startswith("bearer") else f"bearer{token}"
        # 多账号场景必须带发起账号自己的 deviceId；缺省时 build_native_app_headers
        # 会兜底到主号设备号，token/设备不一致会触发风控（check/report 报"网络开小差"）
        self.device_id = (device_id or "").strip() or None
        self.session = requests.Session()

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """走已验证的 App 原生签名体系请求分享/广场接口。"""
        extra_headers = kwargs.pop("extra_headers", {})
        json_body = kwargs.pop("json", None)
        body = None
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        url = BASE_URL + path
        resp = request_with_retry(
            self.session, method, url,
            build_headers=lambda: {
                **build_ios_signature(
                    method, path, token=self.token, query=kwargs.get("params"),
                    accept="application/json", content_type="application/json; charset=UTF-8",
                    body=body,
                ),
                "ca_version": "1",
                "x-requiretoken": "false",
                **build_native_app_headers(device_id=self.device_id, token=self.token),
                # 分享上报仍要求 H5 落地页 Origin；这不是 H5 签名。
                "Origin": "https://h5.lynkco.cn",
                "Referer": "https://h5.lynkco.cn/",
                **extra_headers,
            },
            data=body,
            **kwargs,
        )
        try:
            resp.json()
        except ValueError:
            print(
                f"[警告][H5 {method} {path}] 接口未返回有效 JSON，HTTP {resp.status_code}，"
                f"响应体前200字符: {resp.text[:200]!r}"
            )
        return resp

    def _native_request(self, method: str, path: str, extra_headers: dict = None, **kwargs) -> requests.Response:
        """
        走公共 iOS 原生签名门面请求
        app-api-gw-toc.lynkco.com 网关的部分端点（如 getShareCode）。
        """
        extra = extra_headers or {}
        url = BASE_URL + path
        resp = request_with_retry(
            self.session, method, url,
            build_headers=lambda: {
                **build_ios_signature(
                    method, path, token=self.token, query=kwargs.get("params"),
                    accept="application/json", content_type="application/json; charset=UTF-8",
                ),
                "ca_version": "1",
                "appversion": APP_VERSION,
                **build_native_app_headers(device_id=self.device_id, token=self.token),
                **extra,
            },
            **kwargs,
        )
        try:
            resp.json()
        except ValueError:
            print(
                f"[警告][Native {method} {path}] 接口未返回有效 JSON，HTTP {resp.status_code}，"
                f"响应体前200字符: {resp.text[:200]!r}"
            )
        return resp

    def get_latest_article(self) -> dict:
        """翻页查找探索广场文章流，命中第一篇"文章"即返回 {"articleId", "title"}，失败/未找到时返回空 dict 并打印警告日志。"""
        for page_no in range(1, EXPLORE_SQUARE_PAGE_COUNT + 1):
            try:
                body = {"dynamicSort": "new", "uniqueId": "", "refreshType": "MORE", "pageNo": page_no}
                resp = self._request("POST", EP_EXPLORE_SQUARE_INDEX, json=body)
                resp_json = resp.json()
                found = _find_article(resp_json)
                if found:
                    return found
                print(
                    f"[警告] get_latest_article: 第 {page_no} 页未命中\"文章\"类型内容，"
                    f"接口返回 code={resp_json.get('code')!r}"
                )
            except Exception as e:
                print(f"[警告] get_latest_article: 第 {page_no} 页请求异常: {e}")
                continue
        print(f"[警告] get_latest_article: 翻完 {EXPLORE_SQUARE_PAGE_COUNT} 页仍未找到文章，将回退到 DEFAULT_SHARE_ARTICLE_ID")
        return {}

    def get_share_code(self, article_id: str = None, account_id: str = None) -> dict:
        """
        获取本次分享专属的一次性 shareCode（GET /app/v1/task/getShareCode，走原生
        AppKey 签名）。请求头 risk_request_info 里携带了被分享文章的 id，之后必须原样
        作为 businessNo 传给 share_reporting()，网关会校验两者一致，否则虽返回 success
        但不会真正加分。

        参数:
            article_id: 被分享文章/内容的 id，不传则用 DEFAULT_SHARE_ARTICLE_ID 兜底（推荐
                通过 do_share() 调用，会自动取最新文章）。
            account_id: 当前账号 accountId，用于填充 gl_user_id 风控头，不传则留空。
        """
        article_id = article_id or DEFAULT_SHARE_ARTICLE_ID
        share_content_url = article_share_url(article_id)
        risk_request_info = json.dumps(
            {
                "openTimeStamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "shareContentType": 1,
                "shareContentURL": share_content_url,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        sweet_security_info = build_ios_security_info(device_id=self.device_id)
        extra_headers = {
            "risk_type": "1",
            "risk_request_info": risk_request_info,
            "sweet_security_info": sweet_security_info,
            "os": "13",
        }
        if account_id:
            extra_headers["gl_user_id"] = account_id
        resp = self._native_request("GET", EP_GET_SHARE_CODE, extra_headers=extra_headers)
        return resp.json()

    def share_reporting(self, share_code: str, business_no: str = None,
                         first_classification: str = "文章", second_classification: str = "") -> dict:
        """
        分享上报（POST /app/v1/task/shareReporting?shareCode=<shareCode>，Origin 需为
        https://h5.lynkco.com）。网关会校验 businessNo 与 getShareCode 时的文章 id 是否
        一致。返回 success 不代表真正加分成功，需自行对比 myEnergy 的 point 字段确认。
        """
        business_no = business_no or DEFAULT_SHARE_ARTICLE_ID
        body = {
            "businessNo": business_no,
            "eventData": {
                "firstClassification": first_classification,
                "secondClassification": second_classification,
            },
        }
        resp = self._request(
            "POST", EP_SHARE_REPORTING_SIMPLE,
            params={"shareCode": share_code},
            json=body,
            extra_headers={"Origin": H5_SHARE_ORIGIN, "Referer": H5_SHARE_ORIGIN + "/"},
        )
        return resp.json()

    def share_lookup(self, share_code: str) -> dict:
        """（完整三步法-第1步）通过 shareCode 反查分享人 userId"""
        resp = self._request("POST", EP_SHARE_LOOKUP, json={"shareCode": share_code})
        return resp.json()

    def share_check(self, content_id: str, share_code: str) -> dict:
        """（完整三步法-第2步）分享前置校验"""
        resp = self._request("POST", EP_SHARE_CHECK, json={"contentId": content_id, "shareCode": share_code})
        return resp.json()

    def share_report(self, content_id: str, share_code: str) -> dict:
        """（完整三步法-第3步）真正上报分享、加能量体"""
        resp = self._request("POST", EP_SHARE_REPORT, json={"contentId": content_id, "shareCode": share_code})
        return resp.json()

    def do_share(self, article_id: str = None, account_id: str = None,
                 content_id: str = None, use_simple: bool = True) -> dict:
        """
        执行一次完整的分享任务，自动编排 get_share_code -> share_reporting 两步法，
        use_simple=False 或两步法失败且提供了 content_id 时回退到 lookup->check->report
        完整三步法。返回 {"ok": bool, "via": "simple"|"full", "detail": {...}}。

        未显式传入 article_id 时自动调用 get_latest_article() 取最新文章，失败则回退到
        DEFAULT_SHARE_ARTICLE_ID（无标题），分享成功时返回值会带上 "articleId"/"articleTitle"。
        """
        article_title = ""
        if not article_id:
            latest = self.get_latest_article()
            article_id = latest.get("articleId") or DEFAULT_SHARE_ARTICLE_ID
            article_title = latest.get("title", "")
            if latest and not article_title:
                print(f"[警告] do_share: 动态获取到文章 articleId={article_id}，但该内容节点的 title 字段为空")
            elif not latest:
                print(f"[警告] do_share: 未获取到动态文章，回退使用 DEFAULT_SHARE_ARTICLE_ID={article_id}")

        if use_simple:
            try:
                code_info = self.get_share_code(article_id=article_id, account_id=account_id)
                share_code = code_info.get("data") or ""
                if not share_code:
                    raise RuntimeError(f"getShareCode 未返回有效 shareCode: {code_info}")

                result = self.share_reporting(share_code, business_no=article_id)
                if str(result.get("code")) in ("200", "success"):
                    return {
                        "ok": True, "via": "simple",
                        "articleId": article_id, "articleTitle": article_title,
                        "detail": {"getShareCode": code_info, "shareReporting": result},
                    }
                # 后端明确返回"已分享过"之类信息也视为成功（幂等）
                msg = result.get("message", "")
                if any(k in msg for k in ("已分享", "已领取", "今日已", "已结束")):
                    return {
                        "ok": True, "via": "simple",
                        "articleId": article_id, "articleTitle": article_title,
                        "detail": {"getShareCode": code_info, "shareReporting": result},
                    }
            except Exception as e:
                if not content_id:
                    return {"ok": False, "via": "simple", "detail": {"message": f"简化两步法失败: {e}"}}
                # 简化接口异常且提供了 content_id 时继续尝试完整三步法

        share_code_for_full = None
        try:
            share_code_for_full = (self.get_share_code(article_id=article_id, account_id=account_id).get("data") or "")
        except Exception:
            pass

        if not content_id:
            return {"ok": False, "via": "simple", "detail": {"message": "简化两步法未成功，且未提供 content_id 无法回退到完整三步法"}}

        lookup_result = self.share_lookup(share_code_for_full or "")
        check_result = self.share_check(content_id, share_code_for_full or "")
        report_result = self.share_report(content_id, share_code_for_full or "")
        ok = str(report_result.get("code")) in ("200", "success")
        msg = report_result.get("message", "")
        if not ok and any(k in msg for k in ("已分享", "已领取", "今日已", "已结束")):
            ok = True
        return {
            "ok": ok,
            "via": "full",
            "articleId": article_id, "articleTitle": article_title,
            "detail": {"lookup": lookup_result, "check": check_result, "report": report_result},
        }


def run_auto_share(token: str) -> dict:
    """供 lynkco_sign.py / lynkco_daily_tasks.py 调用的便捷封装：执行一次分享，打印结果。"""
    print("\n=== 执行分享任务 ===")
    client = LynkCoShareClient(token)
    try:
        share_result = client.do_share()
        print(json.dumps(mask_sensitive(share_result), ensure_ascii=False, indent=2))
        if share_result.get("ok"):
            print("\n分享任务成功！")
        else:
            print(f"\n分享任务失败: {share_result.get('detail')}")
        return share_result
    except Exception as e:
        print(f"[警告] 分享任务执行失败: {e}")
        return {"ok": False, "detail": {"message": str(e)}}


# ------------------------- H5 落地页浏览上报（互点加分的关键路径） -------------------------
#
# 分享任务（"分享热爱"）的完成条件是"当日首次分享内容且当日被浏览"。实测
# （2026-10-31）：App 原生会话内的自分享上报与代上报（原生网关 appKey
# 203760416 签名）服务端均不计为浏览，taskStatus 保持 0；只有分享落地页
# （H5，app.lynkco.com/app-h5/...）打开时发出的匿名浏览上报才会计入。
# 该 H5 请求走阿里云网关独立 appKey（204644386），HmacSHA256 签名密钥与
# 拼装格式自 H5 前端 bundle（app-h5/dist/web/vendor.*.js 的 buildApiSigature）
# 提取，并已用真实抓包请求逐字节验证签名一致。

H5_GW_BASE_URL = "https://app-api-gw-toc.lynkco.com"
H5_APP_KEY = "204644386"
H5_APP_SECRET = "QCl7udM3PB9cOIOwquwPglikFQnzJRsX"
H5_SHARE_PAGE_ORIGIN = "https://app.lynkco.com"
H5_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 15_2 like Mac OS X) "
         "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.2 Mobile/15E148 Safari/604.1")


def _h5_signed_headers(method: str, path_with_query: str,
                       accept: str = "*/*",
                       content_type: str = "application/json") -> dict:
    """复刻 H5 bundle buildApiSigature：HmacSHA256(base64) over 固定五行 + 头 + 规范化路径。"""
    timestamp = str(int(time.time() * 1000))
    base = {
        "X-Ca-Key": H5_APP_KEY,
        "X-Ca-Nonce": str(uuid.uuid4()),
        "X-Ca-Signature-Method": "HmacSHA256",
        "X-Ca-Timestamp": timestamp,
    }
    # 与 JS 一致：[METHOD, Accept, "", Content-Type, ""] + 各签名头 K:V + 规范化 path?query
    string_to_sign = "\n".join(
        [method.upper(), accept, "", content_type, ""]
        + [f"{k}:{v}" for k, v in base.items()]
        + [path_with_query]
    )
    signature = base64.b64encode(
        hmac.new(H5_APP_SECRET.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")
    headers = dict(base)
    headers.update({
        "X-Ca-Signature-Headers": "X-Ca-Key,X-Ca-Timestamp,X-Ca-Nonce,X-Ca-Signature-Method",
        "X-Ca-Signature": signature,
        "Accept": accept,
        "Content-Type": content_type,
        "User-Agent": H5_UA,
        "Origin": H5_SHARE_PAGE_ORIGIN,
        "Referer": H5_SHARE_PAGE_ORIGIN + "/",
    })
    return headers


def h5_report_view(share_code: str, business_no: str = "",
                   first_classification: str = "文章",
                   second_classification: str = "") -> dict:
    """
    以 H5 落地页访客身份上报一次浏览（POST /app/v1/task/shareReporting?shareCode=...），
    为分享人记一次"被浏览"。匿名调用（H5 网关签名），无需任何账号 token；
    shareCode 由分享人侧 getShareCode 生成。
    """
    path = f"/app/v1/task/shareReporting?shareCode={share_code}"
    body = {
        "businessNo": business_no or "",
        "eventData": {
            "firstClassification": first_classification,
            "secondClassification": second_classification,
        },
    }
    resp = requests.post(
        H5_GW_BASE_URL + path,
        json=body,
        headers=_h5_signed_headers("POST", path),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ------------------------- 双账号互点分享 -------------------------

def _run_three_step_as_visitor(client: LynkCoShareClient, content_id: str,
                               share_code: str) -> dict:
    """以"点开分享链接的访客"身份走完整三步法（lookup -> check -> report），为分享人加分。"""
    detail = {}
    try:
        detail["lookup"] = client.share_lookup(share_code)
        detail["check"] = client.share_check(content_id, share_code)
        detail["report"] = client.share_report(content_id, share_code)
        report = detail["report"] or {}
        ok = str(report.get("code")) in ("200", "success")
        msg = str(report.get("message") or "")
        if not ok and any(k in msg for k in ("已分享", "已领取", "今日已", "已结束")):
            ok = True
        return {"ok": ok, "detail": detail}
    except Exception as e:
        detail["error"] = str(e)
        return {"ok": False, "detail": detail}


def cross_share(token_a: str, token_b: str,
                client_a: LynkCoShareClient = None,
                client_b: LynkCoShareClient = None,
                account_id_a: str = None, account_id_b: str = None,
                device_id_a: str = None, device_id_b: str = None) -> dict:
    """
    双账号互点分享：每个账号各自 getShareCode 出一次分享，由另一账号以访客身份
    走完整三步法上报，从而让两个账号各获得一次"被点击"加分（在单账号自分享
    +5 之外额外增加，受服务端每日上限约束）。

    返回 {"ok", "a_as_sharer", "b_as_sharer"}：
        a_as_sharer = B 点开 A 的链接后 A 号的加分结果；
        b_as_sharer = A 点开 B 的链接后 B 号的加分结果。
    任一步骤失败不影响另一方向继续执行。
    """
    client_a = client_a or LynkCoShareClient(token_a, device_id=device_id_a)
    client_b = client_b or LynkCoShareClient(token_b, device_id=device_id_b)
    result = {"ok": False, "a_as_sharer": None, "b_as_sharer": None}

    # 方向一：A 分享 -> B 点击 -> A 加分
    print("\n=== 互点分享 · B 访客点击 A 的分享 ===")
    try:
        latest_a = client_a.get_latest_article()
        article_a = latest_a.get("articleId") or DEFAULT_SHARE_ARTICLE_ID
        code_info_a = client_a.get_share_code(article_id=article_a, account_id=account_id_a)
        share_code_a = code_info_a.get("data") or ""
        if not share_code_a:
            raise RuntimeError(f"A getShareCode 未返回 shareCode: {code_info_a}")
        result["a_as_sharer"] = _run_three_step_as_visitor(client_b, article_a, share_code_a)
        result["a_as_sharer"]["articleId"] = article_a
        print(json.dumps(mask_sensitive(result["a_as_sharer"]), ensure_ascii=False, indent=2))
    except Exception as e:
        result["a_as_sharer"] = {"ok": False, "detail": {"message": str(e)}}
        print(f"[警告] 方向一（A 分享/B 点击）失败: {e}")

    # 方向二：B 分享 -> A 点击 -> B 加分
    print("\n=== 互点分享 · A 访客点击 B 的分享 ===")
    try:
        latest_b = client_b.get_latest_article()
        article_b = latest_b.get("articleId") or DEFAULT_SHARE_ARTICLE_ID
        code_info_b = client_b.get_share_code(article_id=article_b, account_id=account_id_b)
        share_code_b = code_info_b.get("data") or ""
        if not share_code_b:
            raise RuntimeError(f"B getShareCode 未返回 shareCode: {code_info_b}")
        result["b_as_sharer"] = _run_three_step_as_visitor(client_a, article_b, share_code_b)
        result["b_as_sharer"]["articleId"] = article_b
        print(json.dumps(mask_sensitive(result["b_as_sharer"]), ensure_ascii=False, indent=2))
    except Exception as e:
        result["b_as_sharer"] = {"ok": False, "detail": {"message": str(e)}}
        print(f"[警告] 方向二（B 分享/A 点击）失败: {e}")

    result["ok"] = bool((result["a_as_sharer"] or {}).get("ok")) and bool((result["b_as_sharer"] or {}).get("ok"))
    return result


def run_cross_share_for_accounts(accounts: list) -> dict:
    """
    供 lynkco_daily_tasks.py 调用：取 load_accounts() 返回的前两个账号互为
    "H5 访客"浏览对方的分享，使每个账号的"分享热爱"任务满足"当日被浏览"条件。
    账号不足 2 个时返回 {"skipped": True}。

    旧版三步法（lookup/check/report，App 原生签名）实测不会被服务端计为浏览，
    已废弃；浏览上报走 h5_report_view（匿名 H5 网关签名，见其 docstring）。
    """
    if not accounts or len(accounts) < 2:
        print("[提示] 账号数量少于 2（需在 env.json 配置 users 数组），跳过互点分享。")
        return {"ok": False, "skipped": True, "reason": "需要至少 2 个账号"}
    from lynkco_login import load_token_for

    tokens = {}
    device_ids = {}
    for index, label in ((0, "A"), (1, "B")):
        account = accounts[index]
        tokens[label] = load_token_for(account, label=account.get("name"))
        device_ids[label] = (account.get("deviceId") or "").strip()

    clients = {
        "A": LynkCoShareClient(tokens["A"], device_id=device_ids.get("A")),
        "B": LynkCoShareClient(tokens["B"], device_id=device_ids.get("B")),
    }

    def _one_direction(sharer: str, visitor: str) -> dict:
        """
        sharer 出分享码并自上报激活（App 真实分享动作的两步），visitor 以 H5
        匿名访客身份上报浏览（服务端只把 H5 通道的上报计为"被浏览"）。
        注意 getShareCode/shareReporting 的 code=success 不代表成功，真实结果
        在 data 字段（"上报成功"/"验证码过期"等）。
        """
        try:
            latest = clients[sharer].get_latest_article()
            article_id = latest.get("articleId") or DEFAULT_SHARE_ARTICLE_ID
            code_info = clients[sharer].get_share_code(article_id=article_id)
            share_code = code_info.get("data") or ""
            if not isinstance(share_code, str) or len(share_code) < 32:
                # 出码失败或触发风控（如 share.need.validate.check 要求极验）
                raise RuntimeError(f"{sharer} getShareCode 未返回有效 shareCode: {code_info}")
            activate = clients[sharer].share_reporting(share_code, business_no=article_id)
            view = h5_report_view(share_code, business_no=article_id)
            view_data = str(view.get("data") or "")
            ok = view_data in ("上报成功",) or any(k in view_data for k in ("已",))
            return {"ok": ok, "detail": {
                "getShareCode": code_info, "activate": activate, "h5ViewReport": view,
            }, "articleId": article_id}
        except Exception as e:
            return {"ok": False, "detail": {"error": str(e)}}

    print("\n=== 互点浏览 · 访客浏览 A（主号）的分享 ===")
    result = {"a_as_sharer": _one_direction("A", "B")}
    print(json.dumps(mask_sensitive(result["a_as_sharer"]), ensure_ascii=False, indent=2))

    print("\n=== 互点浏览 · 访客浏览 B（副号）的分享 ===")
    result["b_as_sharer"] = _one_direction("B", "A")
    print(json.dumps(mask_sensitive(result["b_as_sharer"]), ensure_ascii=False, indent=2))

    result["ok"] = bool(result["a_as_sharer"].get("ok")) and bool(result["b_as_sharer"].get("ok"))
    if result["ok"]:
        print("\n互点浏览：双向均成功！")
    else:
        print("\n互点浏览：存在失败方向，详情见上方日志。")
    return result


# ------------------------- 点赞（真机抓包确认端点） -------------------------

# 抓包确认：POST /app/explore/home-page/article/operation/agree
#   query: itemId=<articleId>&isForward=false|true&type=ThumbCount
#   body : {"path":"pages/exploration/article/index.js?pageCode=LYNKCO_APP_1019&id=<articleId>"}
#   isForward=false 为点赞、true 为取消点赞；响应 {"code":"success","data":true}
EP_LIKE_AGREE = "/app/explore/home-page/article/operation/agree"
LIKE_ORIGIN = "https://app.lynkco.com"


def like_article(client: LynkCoShareClient, article_id: str, like: bool = True) -> dict:
    """给探索广场文章点赞（like=True）或取消点赞（like=False）。"""
    body = {"path": f"pages/exploration/article/index.js?pageCode=LYNKCO_APP_1019&id={article_id}"}
    params = {
        "itemId": article_id,
        "isForward": "false" if like else "true",
        "type": "ThumbCount",
    }
    resp = client._request(
        "POST", EP_LIKE_AGREE, params=params, json=body,
        extra_headers={"Origin": LIKE_ORIGIN, "Referer": LIKE_ORIGIN + "/"},
    )
    result = resp.json()
    ok = str(result.get("code")) in ("200", "success") or bool(result.get("success"))
    print(f"[信息] 点赞 article={article_id} like={like} -> ok={ok}")
    return {"ok": ok, "liked": like, "articleId": article_id, "detail": result}


def like_latest_article(client: LynkCoShareClient) -> dict:
    """取探索广场最新文章点赞一次（成功后不取消，保留互动痕迹）。"""
    latest = client.get_latest_article()
    article_id = latest.get("articleId") or DEFAULT_SHARE_ARTICLE_ID
    return like_article(client, article_id)


def main():
    """独立运行本文件即可单独触发一次分享任务（无需通过 lynkco_sign.py）。"""
    try:
        token = load_token()
    except RuntimeError as e:
        print(f"[错误] {e}")
        sys.exit(1)
    run_auto_share(token)


if __name__ == "__main__":
    main()

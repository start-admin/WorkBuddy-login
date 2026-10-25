#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 手动签到脚本

功能：
  - 账号管理：手动添加（token 方式）、从 dsh-connect-workbuddy 的 vault 目录导入、
    导出 JSON、删除、列表
  - 查看账号当前积分（/v2/billing/meter/get-user-resource）
  - 查看签到状态（/v2/billing/meter/checkin-activity-status，只读）
  - 手动签到（/v2/billing/meter/daily-checkin，先查状态、已签不再重复领取）

接口口径与 dsh-connect-workbuddy 插件保持一致（仅国内版有签到）。
凭据保存在脚本同目录的 accounts.json（权限 600）。

用法示例：
  python3 wb_checkin.py login [cn|global]            # 扫码/授权页登录添加账号
  python3 wb_checkin.py add                          # 交互式添加账号
  python3 wb_checkin.py import <vault目录或json文件>  # 导入账号
  python3 wb_checkin.py list                         # 列出账号
  python3 wb_checkin.py status [账号名]              # 查看积分与签到状态（缺省全部）
  python3 wb_checkin.py checkin [账号名|all]         # 手动签到（缺省全部）
  python3 wb_checkin.py remove <账号名>
  python3 wb_checkin.py export <输出.json>
"""

import argparse
import getpass
import json
import os
import sys
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

import requests

STORE = Path(__file__).resolve().parent / "accounts.json"

# 区域上游（与插件一致：仅国内版有签到）
REGION_BASES = {
    "cn": "https://copilot.tencent.com",
    "global": "https://www.workbuddy.ai",
}
# 从凭据域名推断区域的关键词
GLOBAL_DOMAIN_HINTS = ("workbuddy.ai", "global")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) CodeBuddy/1.0.0 Chrome/136.0.0.0 Electron/37.2.0 Safari/537.36")
TIMEOUT = 20

# OAuth 登录基址（与插件 oauth.ts 一致：CN 的 OAuth 走 codebuddy.cn，
# 国际版默认 workbuddy.ai；与计费基址是两组域名）
OAUTH_BASES = {
    "cn": "https://www.codebuddy.cn",
    "global": "https://www.workbuddy.ai",
}
API_PREFIX = "/v2/plugin"
LOGIN_TIMEOUT_SECONDS = 600  # 单次登录会话有效期，与插件一致


# ---------------------------------------------------------------- 基础 IO

def load_accounts() -> dict:
    if STORE.exists():
        try:
            return json.loads(STORE.read_text("utf-8"))
        except Exception as e:
            sys.exit(f"accounts.json 解析失败：{e}")
    return {}


def save_accounts(accounts: dict) -> None:
    STORE.write_text(json.dumps(accounts, ensure_ascii=False, indent=2), "utf-8")
    try:
        os.chmod(STORE, 0o600)
    except OSError:
        pass


def region_of(domain: str) -> str:
    d = (domain or "").lower()
    return "global" if any(h in d for h in GLOBAL_DOMAIN_HINTS) else "cn"


def base_of(acc: dict) -> str:
    return REGION_BASES[acc.get("region", "cn")]


def headers_of(acc: dict) -> dict:
    base = base_of(acc)
    h = {
        "Authorization": f"Bearer {acc['accessToken']}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": UA,
        "X-Requested-With": "XMLHttpRequest",
        "Origin": base,
        "Referer": base + "/",
    }
    if acc.get("uid"):
        h["X-User-Id"] = acc["uid"]
    if acc.get("enterpriseId"):
        h["X-Enterprise-Id"] = acc["enterpriseId"]
        h["X-Tenant-Id"] = acc["enterpriseId"]
    if acc.get("domain"):
        h["X-Domain"] = acc["domain"]
    return h


def post(acc: dict, path: str, body: dict):
    """返回 (envelope, http_status)；envelope 形如 {code, msg, data}。"""
    resp = requests.post(base_of(acc) + path, headers=headers_of(acc),
                         data=json.dumps(body), timeout=TIMEOUT)
    try:
        env = resp.json()
    except ValueError:
        env = {"code": -1, "msg": f"非 JSON 响应 (HTTP {resp.status_code}): {resp.text[:200]}"}
    return env, resp.status_code


# ---------------------------------------------------------------- 上游只读

def fetch_checkin_status(acc: dict) -> dict:
    env, http = post(acc, "/v2/billing/meter/checkin-activity-status", {})
    if http != 200 or env.get("code") != 0:
        raise RuntimeError(f"签到状态查询失败 (HTTP {http}, code {env.get('code')}): {env.get('msg', '')}")
    d = env.get("data") or {}
    num = lambda k: d[k] if isinstance(d.get(k), (int, float)) else 0
    return {
        "active": d.get("active") is True,
        "todayCheckedIn": d.get("today_checked_in") is True,
        "streakDays": num("streak_days"),
        "dailyCredit": num("daily_credit"),
        "isStreakDay": d.get("is_streak_day") is True,
        "nextStreakDay": num("next_streak_day"),
        "streakBonusCredit": num("streak_bonus_credit"),
    }


def fetch_credits(acc: dict) -> dict:
    """查询积分包余额（个人积分表）。企业账号返回 None 表示走的是另一本账。"""
    now = datetime.now()
    fmt = lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S")
    body = {
        "PageNumber": 1, "PageSize": 100, "ProductCode": "p_tcaca",
        "Status": [0, 3],
        "PackageEndTimeRangeBegin": fmt(now),
        "PackageEndTimeRangeEnd": fmt(now + timedelta(days=366)),
    }
    env, http = post(acc, "/v2/billing/meter/get-user-resource", body)
    if http != 200 or env.get("code") != 0:
        raise RuntimeError(f"积分查询失败 (HTTP {http}, code {env.get('code')}): {env.get('msg', '')}")
    data = ((env.get("data") or {}).get("Response") or {}).get("Data") or {}
    now_ms = time.time() * 1000

    def num(raw: dict, key: str) -> float:
        v = raw.get(key)
        return v if isinstance(v, (int, float)) else 0

    def precise(raw: dict, key: str) -> float:
        """优先读 `*Precise`（含小数），回退整数字段 —— 与插件口径一致。"""
        p = raw.get(f"{key}Precise")
        if isinstance(p, str) and p:
            try:
                return float(p)
            except ValueError:
                pass
        if isinstance(p, (int, float)):
            return p
        return num(raw, key)

    packages = []
    total = 0.0
    nearest = ""
    for raw in data.get("Accounts") or []:
        if not isinstance(raw, dict):
            continue
        # CapacityType 4 = 月度用量资源（每周期刷新、不过期）；1 = 扣减型赠礼
        monthly = num(raw, "CapacityType") == 4
        remain = (precise(raw, "CycleCapacityRemain")
                  if "CycleCapacityRemain" in raw or "CycleCapacityRemainPrecise" in raw
                  else precise(raw, "CapacityRemain"))
        remain = max(remain, 0)
        expiry = raw.get("ExpiredTime") or ("" if monthly else raw.get("CycleEndTime") or "")
        # 丢弃已耗尽/已过期的一次性赠礼；月度资源保留
        if not monthly and (remain <= 0 or
                            (expiry and _ts_ms(expiry) is not None and _ts_ms(expiry) <= now_ms)):
            continue
        total += remain
        if expiry and (not nearest or expiry < nearest):
            nearest = expiry
        packages.append({"remain": remain, "monthly": monthly, "expiry": expiry})
    return {"total": round(total, 2), "packages": packages, "nearestExpiry": nearest}


def _ts_ms(raw) -> float | None:
    """把时间戳字段换算成 epoch ms；数字按 s/ms 容错，字符串按日期解析。"""
    if isinstance(raw, (int, float)) and raw > 0:
        return raw if raw > 1e12 else raw * 1000
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
            return parsed.timestamp() * 1000
        except ValueError:
            try:
                return datetime.fromisoformat(raw).timestamp() * 1000
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------- 动作

def do_checkin(acc: dict) -> str:
    """先查状态再领取；返回人类可读结果。"""
    if acc.get("enterpriseId"):
        return "跳过：企业/教育版账号不支持个人签到"
    status = fetch_checkin_status(acc)
    if not status["active"]:
        return "失败：签到活动未开启"
    if status["todayCheckedIn"]:
        return f"已签到（连续 {status['streakDays']} 天），今日无需重复领取"
    env, http = post(acc, "/v2/billing/meter/daily-checkin", {})
    if http != 200 or env.get("code") != 0:
        return f"签到失败 (HTTP {http}, code {env.get('code')}): {env.get('msg', '')}"
    d = env.get("data") or {}
    credit = d.get("credit") if isinstance(d.get("credit"), (int, float)) else 0
    streak = d.get("streak_days") if isinstance(d.get("streak_days"), (int, float)) else "?"
    return f"✓ 签到成功：+{credit} 积分，连续 {streak} 天"


def show_status(name: str, acc: dict) -> None:
    print(f"\n== {name} [{acc.get('region', 'cn')}]"
          + ("（企业版）" if acc.get("enterpriseId") else ""))
    try:
        c = fetch_credits(acc)
        exp = f"，最近到期 {c['nearestExpiry']}" if c["nearestExpiry"] else ""
        print(f"  积分余额：{c['total']:g}{exp}（{len(c['packages'])} 个可用资源）")
        for p in c["packages"]:
            kind = "月度资源" if p["monthly"] else "赠礼/包"
            print(f"    - {p['remain']:g} 积分 ｜ {kind}"
                  + (f" ｜ 到期/刷新 {p['expiry']}" if p["expiry"] else ""))
    except RuntimeError as e:
        print(f"  积分余额：查询失败 —— {e}")
    try:
        s = fetch_checkin_status(acc)
        state = "已签" if s["todayCheckedIn"] else ("未签" if s["active"] else "活动未开启")
        bonus = f"，连签奖励 +{s['streakBonusCredit']}" if s["streakBonusCredit"] else ""
        print(f"  签到状态：{state} ｜ 连续 {s['streakDays']} 天"
              f" ｜ 每日 {s['dailyCredit']} 积分{bonus}")
    except RuntimeError as e:
        print(f"  签到状态：查询失败 —— {e}")


# ---------------------------------------------------------------- 扫码登录

def _pick(d: dict, keys, default=""):
    """取第一个非空字符串字段（双拼写容错，同插件 firstString）。"""
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v)
    return default


def _expiry_ms(d: dict, abs_keys, ttl_keys):
    for k in abs_keys:
        v = d.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return v * (1 if v > 1e12 else 1000)
    for k in ttl_keys:
        v = d.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return int(time.time() * 1000) + v * 1000
    return 0


def render_qr_in_terminal(url: str) -> bool:
    """尽量在终端画出二维码；没有 qrcode 库就返回 False 退化成链接。"""
    try:
        import qrcode  # pip install qrcode
    except ImportError:
        return False
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.print_ascii(invert=True)
    return True


def cmd_login(args) -> None:
    """OAuth 扫码/授权页登录：申请 state → 用户在浏览器授权 → 轮询换 token。"""
    region = args.region
    base = OAUTH_BASES[region]
    accounts = load_accounts()

    print(f"[{region}] 正在申请登录会话……")
    resp = requests.post(f"{base}{API_PREFIX}/auth/state?platform=workbuddy",
                         headers={"Content-Type": "application/json", "Accept": "application/json"},
                         data="{}", timeout=30)
    try:
        doc = resp.json()
    except ValueError:
        sys.exit(f"登录服务返回非 JSON（HTTP {resp.status_code}）: {resp.text[:200]}")
    data = doc.get("data") if isinstance(doc.get("data"), dict) else {}
    state = _pick(data, ["state", "State"])
    if not state:
        sys.exit(f"登录服务未下发 state (HTTP {resp.status_code}, code {doc.get('code')}): {doc.get('msg', '')}")
    auth_url = _pick(data, ["authUrl", "auth_url", "url", "Url"]) or f"{base}/login?state={state}"

    print(f"\n授权链接（有效期 {LOGIN_TIMEOUT_SECONDS // 60} 分钟）：\n  {auth_url}\n")
    if not render_qr_in_terminal(auth_url):
        print("（未安装 qrcode 库，无法在终端画二维码；可 pip install qrcode，或直接在浏览器打开上面的链接）")
    try:
        webbrowser.open(auth_url)
        print("已尝试自动打开浏览器；若被拦截请手动复制链接，用 WorkBuddy App 扫码或在网页上登录确认。")
    except Exception:
        pass

    print("\n等待授权完成", end="", flush=True)
    deadline = time.time() + LOGIN_TIMEOUT_SECONDS
    token_data = None
    while time.time() < deadline:
        time.sleep(3)
        print(".", end="", flush=True)
        try:
            r = requests.get(f"{base}{API_PREFIX}/auth/token?state={state}",
                             headers={"Accept": "application/json"}, timeout=30)
            doc = r.json()
        except Exception:
            continue  # 网络抖动不算登录失败，继续轮询
        code = doc.get("code", -1)
        if code not in (0, 200):
            continue  # 未完成授权时上游返回业务错误，继续等
        d = doc.get("data") if isinstance(doc.get("data"), dict) else {}
        access = _pick(d, ["accessToken", "access_token", "AccessToken"])
        if access:
            token_data = d
            break
    print()
    if token_data is None:
        sys.exit("登录超时或未完成授权，请重试。")

    # 用 token 拉取账号资料（失败不致命，同插件）
    profile = {}
    profile_headers = {"Authorization": f"Bearer {_pick(token_data, ['accessToken', 'access_token', 'AccessToken'])}",
                       "Accept": "application/json"}
    domain0 = _pick(token_data, ["domain", "Domain"])
    if domain0:
        profile_headers["X-Domain"] = domain0
    try:
        r = requests.get(f"{base}{API_PREFIX}/login/account", headers=profile_headers, timeout=30)
        doc = r.json()
        if isinstance(doc.get("data"), dict):
            profile = doc["data"]
    except Exception:
        pass

    access = _pick(token_data, ["accessToken", "access_token", "AccessToken"])
    refresh = _pick(token_data, ["refreshToken", "refresh_token", "RefreshToken"])
    domain = domain0 or _pick(profile, ["domain", "Domain"])
    uid = _pick(profile, ["uid", "Uid", "userId", "UserID"])
    nickname = _pick(profile, ["nickname", "NickName", "ScreenName"])
    enterprise = _pick(profile, ["enterpriseId", "EnterpriseId"])
    expires_at_ms = _expiry_ms(token_data, ["expiresAt", "expires_at"], ["expiresIn", "expires_in"])

    name = nickname or next_name(accounts, "wb")
    accounts[name] = {
        "accessToken": access,
        "refreshToken": refresh,
        "uid": uid,
        "domain": domain,
        "enterpriseId": enterprise or None,
        "region": region_of(domain) if domain else region,
        "expiresAtMs": expires_at_ms,
        "addedAt": int(time.time()),
    }
    save_accounts(accounts)
    print(f"✓ 登录成功，已保存为 {name}（{accounts[name]['region']}）。正在查询状态……")
    show_status(name, accounts[name])


# ---------------------------------------------------------------- 账号管理

def next_name(accounts: dict, base: str) -> str:
    i = 1
    while f"{base}{i}" in accounts:
        i += 1
    return f"{base}{i}"


def cmd_add(args) -> None:
    accounts = load_accounts()
    print("添加账号（token 来自本机 WorkBuddy 登录态，可参考 dsh-connect-workbuddy vault 文件）")
    token = getpass.getpass("  accessToken: ").strip()
    if not token:
        sys.exit("token 不能为空")
    uid = input("  uid (X-User-Id，可留空): ").strip()
    domain = input("  domain (可留空): ").strip()
    enterprise = input("  enterpriseId (企业/教育版才填，可留空): ").strip()
    nickname = input("  备注名 (可留空自动编号): ").strip()
    region = "global" if any(h in domain.lower() for h in GLOBAL_DOMAIN_HINTS) else "cn"
    name = nickname or next_name(accounts, "wb")
    accounts[name] = {
        "accessToken": token, "uid": uid, "domain": domain,
        "enterpriseId": enterprise or None, "region": region,
        "addedAt": int(time.time()),
    }
    save_accounts(accounts)
    print(f"已保存为 {name}，正在验证……")
    try:
        show_status(name, accounts[name])
    except Exception as e:
        print(f"验证未通过：{e}")


def cmd_import(args) -> None:
    accounts = load_accounts()
    src = Path(args.source)
    files: list[Path] = []
    if src.is_dir():
        files = sorted(src.rglob("*.json"))
    elif src.is_file():
        files = [src]
    else:
        sys.exit(f"路径不存在：{src}")
    imported = skipped = 0
    for f in files:
        try:
            doc = json.loads(f.read_text("utf-8"))
        except Exception:
            skipped += 1
            continue
        # 两种格式：插件 vault 文档（{auth:{...}, account:{...}}）或导出 JSON（{name: acc}）
        if "auth" in doc and isinstance(doc["auth"], dict):
            auth, acct = doc["auth"], doc.get("account") or {}
            token = auth.get("accessToken")
            if not token:
                skipped += 1
                continue
            domain = auth.get("domain") or ""
            name = acct.get("nickname") or acct.get("displayName") or next_name(accounts, "wb")
            accounts[name] = {
                "accessToken": token,
                "refreshToken": auth.get("refreshToken") or "",
                "uid": acct.get("uid") or "",
                "domain": domain,
                "enterpriseId": acct.get("enterpriseId"),
                "region": region_of(domain),
                "addedAt": int(time.time()),
            }
            imported += 1
        elif all(isinstance(v, dict) and "accessToken" in v for v in doc.values()):
            for name, acc in doc.items():
                key = name
                i = 2
                while key in accounts:
                    key = f"{name}#{i}"; i += 1
                acc.setdefault("region", region_of(acc.get("domain", "")))
                accounts[key] = acc
                imported += 1
        else:
            skipped += 1
    save_accounts(accounts)
    print(f"导入完成：成功 {imported} 个，跳过 {skipped} 个。当前共 {len(accounts)} 个账号。")


def cmd_list(args) -> None:
    accounts = load_accounts()
    if not accounts:
        print("还没有账号，先用 add 或 import 添加。")
        return
    print(f"共 {len(accounts)} 个账号：")
    for name, acc in accounts.items():
        tag = acc.get("region", "cn")
        ent = "（企业版）" if acc.get("enterpriseId") else ""
        print(f"  - {name}  [{tag}]{ent}  uid={acc.get('uid') or '-'}")


def cmd_status(args) -> None:
    accounts = load_accounts()
    if args.name:
        if args.name not in accounts:
            sys.exit(f"账号不存在：{args.name}")
        show_status(args.name, accounts[args.name])
        return
    for name, acc in accounts.items():
        show_status(name, acc)


def cmd_checkin(args) -> None:
    accounts = load_accounts()
    if args.name and args.name != "all":
        if args.name not in accounts:
            sys.exit(f"账号不存在：{args.name}")
        targets = {args.name: accounts[args.name]}
    else:
        targets = accounts
    if not targets:
        sys.exit("没有可签到的账号。")
    for i, (name, acc) in enumerate(targets.items()):
        if i > 0:
            time.sleep(2)  # 请求间隔，避免过快
        print(f"[{name}] {do_checkin(acc)}")


def cmd_remove(args) -> None:
    accounts = load_accounts()
    if args.name not in accounts:
        sys.exit(f"账号不存在：{args.name}")
    del accounts[args.name]
    save_accounts(accounts)
    print(f"已删除 {args.name}")


def cmd_export(args) -> None:
    accounts = load_accounts()
    Path(args.out).write_text(json.dumps(accounts, ensure_ascii=False, indent=2), "utf-8")
    print(f"已导出 {len(accounts)} 个账号到 {args.out}（注意文件包含明文 token）")


def main() -> None:
    parser = argparse.ArgumentParser(description="WorkBuddy 手动签到脚本")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("login", help="扫码/授权页登录（cn=国内版，global=国际版）")
    p.add_argument("region", nargs="?", default="cn", choices=["cn", "global"])
    p.set_defaults(func=cmd_login)
    sub.add_parser("add", help="交互式添加账号").set_defaults(func=cmd_add)
    p = sub.add_parser("import", help="从 vault 目录或导出 JSON 导入账号")
    p.add_argument("source"); p.set_defaults(func=cmd_import)
    sub.add_parser("list", help="列出账号").set_defaults(func=cmd_list)
    p = sub.add_parser("status", help="查看积分与签到状态")
    p.add_argument("name", nargs="?", default=None); p.set_defaults(func=cmd_status)
    p = sub.add_parser("checkin", help="手动签到（缺省全部账号）")
    p.add_argument("name", nargs="?", default=None); p.set_defaults(func=cmd_checkin)
    p = sub.add_parser("remove", help="删除账号")
    p.add_argument("name"); p.set_defaults(func=cmd_remove)
    p = sub.add_parser("export", help="导出账号 JSON")
    p.add_argument("out"); p.set_defaults(func=cmd_export)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

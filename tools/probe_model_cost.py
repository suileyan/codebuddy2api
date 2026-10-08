"""实测探测：某个模型在国内站 / 国际站各消耗多少 credit。

⚠️ 与 probe_site_credits.py 不同，**本脚本会真实消耗上游额度**（对收费模型而言）。
它对应 workbuddy-gateway 的 `probe` 命令：目录接口只列客户端展示用的清单，
会漏模型也会过期，只有实测 usage.credit 才是计费真相。

判定规则与调度器一致：
  credit > 0                → 收费
  credit == 0 且 tokens≥300 → 免费
  credit == 0 但样本不足    → 不判定（上游对极小请求也会记 0）

用法：
    .venv/Scripts/python.exe tools/probe_model_cost.py deepseek-v4.1-flash glm-5.3-flash
    .venv/Scripts/python.exe tools/probe_model_cost.py --site intl deepseek-v4-pro
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from core.converter import CredentialManager  # noqa: E402

# 与 admin/pool.py 的 MODEL_FREE_MIN_TOKENS 保持一致。
FREE_MIN_TOKENS = 300
# 探测请求要足够长：短回答的 total_tokens 会低于阈值，判定不出来。
PROBE_PROMPT = ("Please write a factual essay of about 400 words about the water cycle, "
                "covering evaporation, condensation, precipitation and runoff. "
                "Write in continuous prose, without markdown or headings.")
PROBE_MAX_TOKENS = 1200


def load_env() -> None:
    env = ROOT / "deploy" / "local" / ".env"
    if not env.is_file():
        return
    for raw in env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def accounts_for(auth_dir: Path, site: str) -> list[Path]:
    """该站点的全部凭据，按文件名排序。"""
    found = []
    for path in sorted(auth_dir.glob("*.info")):
        try:
            manager = CredentialManager(path)
            resolved = "intl" if manager.edition() in {"intl", "international", "global", "workbuddy.ai"} else "cn"
        except Exception:  # noqa: BLE001
            continue
        if resolved == site:
            found.append(path)
    return found


def read_usage(text: str) -> dict | None:
    usage = None
    for line in text.splitlines():
        if line.startswith("data:") and "usage" in line:
            try:
                obj = json.loads(line[5:].strip())
            except ValueError:
                continue
            if isinstance(obj.get("usage"), dict):
                usage = obj["usage"]
    return usage


def probe(path: Path, model: str) -> dict:
    manager = CredentialManager(path)
    headers = dict(manager.get_headers())
    headers["X-Client-Platform"] = "web"
    body = {"model": model, "stream": True, "max_tokens": PROBE_MAX_TOKENS,
            "messages": [{"role": "system", "content": "You are a helpful assistant."},
                         {"role": "user", "content": PROBE_PROMPT}]}
    try:
        with httpx.Client(timeout=180) as client:
            resp = client.post(manager.backend() + "/v2/chat/completions", headers=headers, json=body)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"请求失败：{exc}"}
    if resp.status_code >= 400:
        try:
            payload = resp.json()
            msg = payload.get("msg") or payload.get("displayMsg") or resp.text[:120]
            code = payload.get("code")
        except ValueError:
            msg, code = resp.text[:120], None
        return {"error": f"HTTP {resp.status_code} code={code} {msg}"}
    usage = read_usage(resp.text)
    if not usage:
        return {"error": "响应中没有 usage"}
    credit = usage.get("credit")
    tokens = usage.get("total_tokens") or 0
    if credit is None:
        verdict = "无 credit 字段"
    elif credit > 0:
        verdict = "收费"
    elif tokens >= FREE_MIN_TOKENS:
        verdict = "免费"
    else:
        verdict = "样本不足，不判定"
    return {"credit": credit, "total_tokens": tokens, "verdict": verdict}


def push_ledger(base_url: str, admin_key: str, model_cost: dict) -> None:
    """把实测结果写进正在运行的管理后台，立即影响调度。

    登录态 cookie 带 Secure 标记，httpx 不会在 http 上回传，所以这里从
    Set-Cookie 里取出会话值，用显式 Cookie 头带上（仅本机回环调用）。
    """
    with httpx.Client(timeout=30) as client:
        resp = client.post(base_url + "/admin/api/login", json={"key": admin_key})
        if resp.status_code != 200:
            raise SystemExit(f"管理后台登录失败：HTTP {resp.status_code} {resp.text[:120]}")
        csrf = resp.json().get("csrf")
        raw_cookie = resp.headers.get("set-cookie", "")
        name, _, rest = raw_cookie.partition("=")
        session = rest.split(";", 1)[0]
        if not name or not session:
            raise SystemExit("管理后台没有返回登录 cookie，无法写入。")
    with httpx.Client(timeout=30) as client:
        resp = client.post(base_url + "/admin/api/model-cost",
                           headers={"Cookie": f"{name}={session}", "X-CSRF-Token": csrf},
                           json={"model_cost": model_cost})
        if resp.status_code != 200:
            raise SystemExit(f"写入账本失败：HTTP {resp.status_code} {resp.text[:160]}")
        data = resp.json()
        print(f"已写入账本：更新 {data['updated']} 条，忽略 {data['rejected']} 条")
        print("当前账本：", json.dumps(data["model_cost"], ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="实测模型在两站的计费（会消耗上游额度）")
    parser.add_argument("models", nargs="+", help="模型 id")
    parser.add_argument("--site", choices=["cn", "intl", "both"], default="both")
    parser.add_argument("--write", action="store_true",
                        help="把实测结果写入正在运行的管理后台，立即影响调度（需服务已启动）")
    parser.add_argument("--base-url", default=None, help="管理后台地址，默认取 .env 的 HOST/PORT")
    parser.add_argument("--admin-key", default=None, help="管理密钥，默认取 .env 的 ADMIN_KEY")
    args = parser.parse_args()

    load_env()
    auth_dir = Path(os.environ.get("CODEBUDDY_AUTH_DIR", ROOT / "deploy" / "local" / "auth"))
    sites = ["cn", "intl"] if args.site == "both" else [args.site]

    print(f"凭据目录：{auth_dir}")
    print("注意：本命令会真实消耗上游额度（收费模型按实际用量计费）。\n")

    summary: dict[str, dict] = {}
    for model in args.models:
        summary[model] = {}
        for site in sites:
            paths = accounts_for(auth_dir, site)
            if not paths:
                print(f"{model:24} {site:5} 跳过：没有该站点的凭据")
                continue
            result, used = None, None
            # 零余额账号连免费模型也会被上游整体拒绝（14018），逐个换账号重试。
            for path in paths:
                attempt = probe(path, model)
                if attempt.get("error", "").startswith("HTTP 429"):
                    print(f"{model:24} {site:5} {path.name} 额度耗尽，换下一个账号")
                    continue
                result, used = attempt, path
                break
            if result is None:
                result, used = {"error": "该站点全部账号额度耗尽"}, paths[-1]
            summary[model][site] = result
            if "error" in result:
                print(f"{model:24} {site:5} {result['error']}")
            else:
                print(f"{model:24} {site:5} credit={result['credit']!s:8} "
                      f"tokens={result['total_tokens']:5} -> {result['verdict']}  [{used.name}]")
        print()

    free, paid = {}, {}
    for model, per_site in summary.items():
        f = [s for s, r in per_site.items() if r.get("verdict") == "免费"]
        p = [s for s, r in per_site.items() if r.get("verdict") == "收费"]
        if f:
            free[model] = f
        if p:
            paid[model] = p
    actionable = {m: {"free": free[m], "paid": paid.get(m, [])} for m in free if paid.get(m)}
    if actionable:
        print("可省钱的模型（一边免费、一边收费，调度器会自动优先免费站）：")
        for model, info in actionable.items():
            print(f"  {model}  →  优先 {', '.join(info['free'])}（{', '.join(info['paid'])} 收费）")
    else:
        print("本次探测没有发现「一边免费一边收费」的模型。")

    if args.write:
        ledger = {}
        for model, per_site in summary.items():
            sites_map = {site: ("free" if r.get("verdict") == "免费" else "paid")
                         for site, r in per_site.items() if r.get("verdict") in ("免费", "收费")}
            if sites_map:
                ledger[model] = sites_map
        if not ledger:
            raise SystemExit("\n没有可写入的判定结果。")
        admin_key = args.admin_key or os.environ.get("ADMIN_KEY")
        if not admin_key:
            raise SystemExit("\n缺少管理密钥：请用 --admin-key 指定，或在 deploy/local/.env 中配置 ADMIN_KEY。")
        base_url = args.base_url or f"http://{os.environ.get('HOST', '127.0.0.1')}:{os.environ.get('PORT', '8787')}"
        print()
        push_ledger(base_url.rstrip("/"), admin_key, ledger)


if __name__ == "__main__":
    main()

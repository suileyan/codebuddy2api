"""把外部项目的凭据导入本部署的 auth 目录，并注册到管理后台。

用法：
    .venv/Scripts/python.exe deploy/local/import_accounts.py <源目录> [<源目录> ...]

处理内容：
- 时间戳归一化：expiresAt / refreshExpiresAt / lastRefreshTime 统一为毫秒
  （本项目按毫秒判定过期，秒级时间戳会被误判为已过期）
- 凭据写入 deploy/local/auth/，文件名沿用来源文件名 + .info
- 重建 management/state.json 的账号列表：按 uid 去重，保留 keys / pool 等配置
- **复制**而非移动，源目录不受影响

注意：运行前先停掉服务，否则 state.json 会被服务进程覆盖。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

LOCAL_DIR = Path(__file__).resolve().parent
ROOT = LOCAL_DIR.parents[1]
AUTH_DIR = LOCAL_DIR / "auth"
STATE = ROOT / "management" / "state.json"

TIME_KEYS = ("expiresAt", "refreshExpiresAt", "lastRefreshTime")


def to_ms(value):
    """秒级时间戳转毫秒；已是毫秒或非法值则原样返回。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return value
    return int(value * 1000) if value < 10**12 else int(value)


def normalize(doc: dict) -> dict:
    auth = doc.get("auth")
    if isinstance(auth, dict):
        for key in TIME_KEYS:
            if key in auth:
                auth[key] = to_ms(auth[key])
    return doc


def load_state() -> dict:
    if STATE.is_file():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"accounts": {}, "active": None, "keys": {}}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2

    sources = [Path(p) for p in argv[1:]]
    missing = [str(p) for p in sources if not p.is_dir()]
    if missing:
        print("源目录不存在：" + ", ".join(missing))
        return 1

    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    existing_keys = state.get("keys", {})
    existing_pool = state.get("pool")

    accounts: dict[str, dict] = {}
    seen: dict[str, str] = {}  # uid -> aid，用于去重

    for src in sources:
        for path in sorted(list(src.glob("*.json")) + list(src.glob("*.info"))):
            try:
                doc = normalize(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, OSError) as exc:
                print(f"  跳过 {path.name}：{exc}")
                continue

            account = doc.get("account")
            auth = doc.get("auth")
            if not isinstance(account, dict) or not isinstance(auth, dict):
                print(f"  跳过 {path.name}：缺少 account / auth")
                continue

            uid = str(account.get("uid") or "")
            if not uid:
                print(f"  跳过 {path.name}：缺少 account.uid")
                continue
            if not auth.get("accessToken") or not auth.get("refreshToken"):
                print(f"  跳过 {path.name}：缺少 token")
                continue

            name = str(account.get("nickname") or uid)[:60]
            target = AUTH_DIR / (path.stem + ".info")

            if uid in seen:
                aid = seen[uid]
                print(f"  合并重复 uid {uid[:8]}：{path.name} 覆盖 {accounts[aid]['file']}")
            else:
                aid = __import__("secrets").token_hex(8)
                seen[uid] = aid

            target.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            accounts[aid] = {
                "file": target.name,
                "name": name,
                "created": int(time.time()),
                "enabled": True,
            }

    if not accounts:
        print("没有导入任何账号。")
        return 1

    first = next(iter(accounts))
    state.update(
        {
            "accounts": accounts,
            "active": first,
            "keys": existing_keys,
            "account_status": {},
            "session_bindings": {},
        }
    )
    if existing_pool:
        state["pool"] = existing_pool

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n已导入 {len(accounts)} 个账号到 {AUTH_DIR}")
    for aid, item in accounts.items():
        mark = " (active)" if aid == first else ""
        print(f"  {item['name']}{mark}")
    print(f"\nstate.json 已更新：{STATE}")
    print("提醒：请在 deploy/local/.env 中把 CODEBUDDY_AUTH_DIR 指向该 auth 目录。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

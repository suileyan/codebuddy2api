"""只读探测：查询国内站 / 国际站的模型倍率目录。

对应 workbuddy-gateway 里的 GET {Base}/v2/enterprises/personal/models，
用于回答「同一个模型在哪个站免费」。不发聊天请求、不改动任何状态。

用法：
    .venv/Scripts/python.exe tools/probe_site_credits.py
    .venv/Scripts/python.exe tools/probe_site_credits.py deepseek-v4.1-flash hy3
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from core.converter import CredentialManager  # noqa: E402

MODELS_PATH = "/v2/enterprises/personal/models"


def load_env() -> None:
    env = ROOT / "deploy" / "local" / ".env"
    if not env.is_file():
        return
    for raw in env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def probe(path: Path, wanted: list[str]) -> dict:
    mgr = CredentialManager(path)
    try:
        headers = mgr.get_headers()
    except Exception as exc:  # noqa: BLE001
        return {"file": path.name, "error": f"读取凭据失败：{exc}"}
    base = mgr.backend()
    url = base + MODELS_PATH
    try:
        with httpx.Client(timeout=25, follow_redirects=False) as client:
            resp = client.get(url, headers=headers)
    except Exception as exc:  # noqa: BLE001
        return {"file": path.name, "edition": mgr.edition(), "base": base, "error": f"请求失败：{exc}"}
    if resp.status_code != 200:
        return {"file": path.name, "edition": mgr.edition(), "base": base,
                "error": f"HTTP {resp.status_code}", "body": resp.text[:300]}
    try:
        doc = resp.json()
    except ValueError:
        return {"file": path.name, "edition": mgr.edition(), "base": base, "error": "返回非 JSON"}
    # 上游把目录包在 {"code":0,"data":{...}} 里，models / modelPromotions 都在 data 下。
    if isinstance(doc, dict) and isinstance(doc.get("data"), dict):
        doc = doc["data"]
    models = doc.get("models") or []
    promos = doc.get("modelPromotions") or []
    table = {}
    for m in models:
        mid = str(m.get("id") or "").strip()
        if mid:
            table[mid] = m.get("credits")
    picked = {mid: table.get(mid) for mid in wanted}
    return {
        "file": path.name,
        "edition": mgr.edition(),
        "base": base,
        "model_count": len(models),
        "promotions": len(promos),
        "wanted": picked,
        "promo_detail": [
            {"ids": p.get("modelIds"), "factor": (p.get("discount") or {}).get("factor"),
             "enabled": p.get("enabled"), "label": (p.get("badge") or {}).get("label"),
             "validUntil": (p.get("schedule") or {}).get("validUntil")}
            for p in promos
            if set(p.get("modelIds") or []) & set(wanted)
        ],
    }


def main() -> None:
    load_env()
    wanted = sys.argv[1:] or ["deepseek-v4.1-flash", "hy3", "glm-5.3", "gpt-5.1-codex"]
    auth_dir = Path(os.environ.get("CODEBUDDY_AUTH_DIR", ROOT / "deploy" / "local" / "auth"))
    files = sorted(auth_dir.glob("*.info"))
    if not files:
        raise SystemExit(f"未找到凭据文件：{auth_dir}")
    print(f"凭据目录：{auth_dir}\n关注模型：{wanted}\n")
    for path in files:
        result = probe(path, wanted)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("-" * 72)


if __name__ == "__main__":
    main()

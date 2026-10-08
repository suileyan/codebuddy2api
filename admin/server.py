"""Small, persistent management layer for workbuddy2api. Single-process deployment."""
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from core import converter
from core.workbuddy_atrest_crypto import decrypt_auth_field, is_encrypted_field
from .browser_login import BrowserLogin
from .logstore import AGGREGATE_MAX_DAYS, LogStore, parse_day, setup_logging
from .logstore import CN as LOG_TZ
from .pool import AccountPool, PoolMiddleware
from .metrics import RequestMetrics, MetricsMiddleware

COOKIE = "workbuddy_admin"
MAX_BODY = 1024 * 1024
ASSETS = Path(__file__).parent / "static"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def write_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)


def clean_name(value, fallback):
    if value is None:
        return fallback
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 60:
        raise HTTPException(400, "名称需为 1–60 个字符")
    return value.strip()


def display_name(doc, fallback):
    """取可读账号名：明文 nickname → 解密 nickname → uid → fallback。

    WorkBuddy 5.6.0+ 起 nickname 也可能是 $wbEncrypted 信封，解密依赖本机
    WorkBuddy 可执行文件；不可用时降级为 uid，绝不抛错。
    """
    account = doc.get("account") if isinstance(doc, dict) else None
    if isinstance(account, dict):
        nick = account.get("nickname")
        if isinstance(nick, str) and nick.strip():
            return nick.strip()[:60]
        if is_encrypted_field(nick):
            try:
                value = decrypt_auth_field(nick)
            except Exception:  # noqa: BLE001 - 解密失败时降级，不影响导入
                value = ""
            if isinstance(value, str) and value.strip():
                return value.strip()[:60]
        uid = account.get("uid")
        if isinstance(uid, str) and uid:
            return uid[:60]
    return fallback


class Store:
    def __init__(self, root, auth_dir, initial_key, admin_key):
        # 本地部署放宽到 8 位（上游默认 20 位）。服务仅监听回环地址，
        # 且登录接口有同 IP 10 分钟内 8 次失败的限流兜底。
        if len(admin_key) < 8:
            raise RuntimeError("ADMIN_KEY must contain at least 8 characters")
        self.root, self.auth_dir = Path(root), Path(auth_dir)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.auth_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.admin_digest = digest(admin_key)
        self.sessions, self.attempts = {}, {}
        self.test_lock = asyncio.Lock()
        self.test_keys = set()
        self.managers = {}
        self.started = time.time()
        self.events = deque(maxlen=40)
        self.path = self.root / "state.json"
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {"accounts": {}, "active": None, "keys": {}}
            for p in sorted(self.auth_dir.glob("*.info")):
                try:
                    doc = json.loads(p.read_text(encoding="utf-8"))
                    self.validate_credential(doc)
                except (ValueError, OSError, HTTPException):
                    continue
                aid = secrets.token_hex(8)
                name = display_name(doc, "已迁移账号")
                self.data["accounts"][aid] = {"name": name, "file": p.name, "enabled": not bool(doc.get("disabled")), "created": int(time.time())}
                if self.data["active"] is None and not doc.get("disabled"):
                    self.data["active"] = aid
            if initial_key:
                self.add_key("原有 API Key", initial_key)
            self.save()
        self.sync_active()

    @staticmethod
    def validate_credential(doc):
        if not isinstance(doc, dict) or not isinstance(doc.get("auth"), dict) or not isinstance(doc.get("account"), dict):
            raise HTTPException(400, "需要桌面端 .info／JSON 登录文件，包含 auth 和 account 对象")
        for key in ("accessToken", "refreshToken"):
            value = doc["auth"].get(key)
            # WorkBuddy 5.6.0+ 起 token 变为 {"$wbEncrypted":1,"envelope":...} 信封，
            # 运行时由 CredentialManager 解密，这里只校验信封结构完整。
            if is_encrypted_field(value):
                if len(value.get("envelope", "")) > 65536:
                    raise HTTPException(400, "凭据字段过长")
                continue
            if not isinstance(value, str) or not value.strip():
                raise HTTPException(400, "凭据缺少 accessToken 或 refreshToken，请重新导出桌面端登录文件")
            if len(value) > 65536:
                raise HTTPException(400, "凭据字段过长")
        if not isinstance(doc["account"].get("uid"), str) or not doc["account"]["uid"]:
            raise HTTPException(400, "凭据缺少 account.uid")
        if not isinstance(doc["auth"].get("expiresAt"), (int, float)) or isinstance(doc["auth"].get("expiresAt"), bool):
            raise HTTPException(400, "凭据需要有效的 expiresAt 时间戳")
        for key in ("domain",):
            value = doc["auth"].get(key, "")
            if not isinstance(value, str) or "\r" in value or "\n" in value:
                raise HTTPException(400, "凭据包含无效字段")
        for key in ("uid", "enterpriseId"):
            value = doc["account"].get(key, "")
            if not isinstance(value, str) or "\r" in value or "\n" in value:
                raise HTTPException(400, "账号字段格式不正确")

    def save(self):
        write_json(self.path, self.data)

    def save_browser_account(self, doc, name):
        self.validate_credential(doc)
        with self.lock:
            identity = (doc["account"]["uid"], doc["account"].get("enterpriseId", ""))
            for aid, item in self.data["accounts"].items():
                try:
                    old = json.loads(self.file_for(item).read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                if (old.get("account", {}).get("uid"), old.get("account", {}).get("enterpriseId", "")) == identity:
                    current = self.manager_for(aid, item)
                    if current:
                        with current._lock:
                            write_json(self.file_for(item), doc)
                            current._cached = None
                    else:
                        write_json(self.file_for(item), doc)
                    if name:
                        item["name"] = name
                    self.data.get("account_status", {}).pop(aid, None)
                    self.save()
                    self.sync_active()
                    return {"account_id": aid, "updated": True}
            if len(self.data["accounts"]) >= 100:
                raise HTTPException(400, "最多保存 100 个账号")
            aid = secrets.token_hex(8)
            write_json(self.auth_dir / (aid + ".info"), doc)
            self.data["accounts"][aid] = {"file": aid + ".info", "name": name or display_name(doc, "浏览器授权账号"), "created": int(time.time()), "enabled": True}
            if self.data["active"] is None:
                self.data["active"] = aid
            self.save()
            self.sync_active()
            return {"account_id": aid, "updated": False}

    def file_for(self, item):
        path = self.auth_dir / item["file"]
        if path.parent.resolve() != self.auth_dir.resolve() or path.is_symlink():
            raise HTTPException(400, "凭据路径不合法")
        return path

    def sync_active(self):
        aid = self.data.get("active")
        item = self.data["accounts"].get(aid)
        converter.CONFIG["cred"] = self.manager_for(aid, item) if item and item["enabled"] and self.file_for(item).exists() else None

    def manager_for(self, aid, item):
        with self.lock:
            if aid not in self.managers:
                self.managers[aid] = converter.CredentialManager(self.file_for(item))
            return self.managers[aid]

    def account_rows(self):
        result = []
        for aid, item in self.data["accounts"].items():
            row = {"id": aid, "name": item["name"], "enabled": item["enabled"], "active": aid == self.data["active"], "created": item["created"]}
            try:
                doc = json.loads(self.file_for(item).read_text(encoding="utf-8"))
                expiry = doc["auth"].get("expiresAt", 0)
                row.update({"nickname": display_name(doc, ""), "uid": str(doc["account"].get("uid") or ""), "expires_at": expiry, "expired": expiry < time.time() * 1000, "refresh_available": bool(doc["auth"].get("refreshToken")), "status": "ready"})
            except (ValueError, OSError, KeyError):
                row.update({"status": "invalid", "expired": True, "expires_at": 0, "refresh_available": False})
            result.append(row)
        return result

    def add_key(self, name, value=None):
        value = value or "sk-wb-" + secrets.token_urlsafe(32)
        kid = secrets.token_hex(8)
        self.data["keys"][kid] = {"name": name, "hash": digest(value), "hint": value[:5] + "…" + value[-4:], "created": int(time.time())}
        return kid, value

    def check_api(self, authorization, x_api_key):
        token = authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else x_api_key or ""
        with self.lock:
            hashed = digest(token)
            allowed = hashed in self.test_keys or any(hmac.compare_digest(hashed, item["hash"]) for item in self.data["keys"].values())
        if not allowed:
            raise HTTPException(401, "invalid api key")

    def key_label(self, token):
        """把客户端 API Key 映射成便于识别的一行文字（名称 · 掩码）。

        只用于「最近请求」展示，拿不到时返回 None。不落盘、不记录原始密钥。
        """
        if not token:
            return None
        hashed = digest(token)
        with self.lock:
            if hashed in self.test_keys:
                return "后台测试"
            for item in self.data["keys"].values():
                if hmac.compare_digest(hashed, item["hash"]):
                    name = item.get("name") or "未命名"
                    hint = item.get("hint") or ""
                    return f"{name} · {hint}" if hint else name
        return "未知密钥"

    def require_admin(self, req):
        sid = req.cookies.get(COOKIE, "")
        with self.lock:
            record = self.sessions.get(digest(sid))
            if not record or record["expires"] < time.time():
                raise HTTPException(401, "请先登录管理后台")
        if req.method not in ("GET", "HEAD"):
            if not hmac.compare_digest(req.headers.get("X-CSRF-Token", ""), record["csrf"]):
                raise HTTPException(403, "页面已失效，请重新登录")
        return record


class AdminMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/admin"):
            return await self.app(scope, receive, send)
        messages, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > MAX_BODY:
                response = JSONResponse({"detail": "文件或请求超过 1 MB"}, 413)
                return await response(scope, receive, send)
            messages.append(message)
            if not message.get("more_body", False):
                break
        async def replay():
            return messages.pop(0) if messages else await receive()
        async def secure_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"), (b"x-frame-options", b"DENY"),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
                ]
            await send(message)
        await self.app(scope, replay, secure_send)


def create_app(root=None, auth_dir=None, initial_key=None, admin_key=None, secure_cookie=True):
    store = Store(root or os.environ.get("MANAGEMENT_DATA_DIR", "/data/management"), auth_dir or os.environ.get("CODEBUDDY_AUTH_DIR", "/data/auth"), initial_key if initial_key is not None else os.environ.get("CODEBUDDY2OPENAI_KEY", ""), admin_key or os.environ.get("ADMIN_KEY", ""))
    converter._check_auth = store.check_api
    converter.CONFIG.update({"desensitize": True, "no_compact": False, "log_path": None})
    browser_login = BrowserLogin(store.save_browser_account)
    pool = AccountPool(store)
    logstore = LogStore(root or os.environ.get("MANAGEMENT_DATA_DIR", "/data/management"))
    metrics = RequestMetrics(sink=logstore.append)
    try:
        metrics.restore(logstore.read(limit=100)["records"])
    except Exception:  # noqa: BLE001 - 历史日志损坏时照常启动
        pass
    @asynccontextmanager
    async def lifespan(app):
        async def reap():
            while True:
                await asyncio.sleep(30)
                await browser_login.cleanup()
        cleanup_task = asyncio.create_task(reap())
        async def pool_worker():
            while True:
                try:
                    await pool.tick()
                except Exception:
                    pass  # Per-account errors are persisted without credential data.
                await asyncio.sleep(60)
        pool_task = asyncio.create_task(pool_worker())
        try:
            yield
        finally:
            cleanup_task.cancel()
            pool_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            await browser_login.close()
            try:
                await pool_task
            except asyncio.CancelledError:
                pass
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.browser_login = browser_login
    app.state.pool = pool
    app.state.metrics = metrics
    app.state.logstore = logstore
    app.add_middleware(AdminMiddleware)
    app.add_middleware(PoolMiddleware, pool=pool)
    app.add_middleware(MetricsMiddleware, metrics=metrics, key_lookup=store.key_label)

    async def payload(req):
        try:
            body = await req.json()
        except ValueError:
            raise HTTPException(400, "请求不是有效 JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "请求格式不正确")
        return body

    @app.get("/")
    async def home():
        return RedirectResponse("/admin/", 302)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/admin")
    async def redirect_admin():
        return RedirectResponse("/admin/", 302)

    @app.get("/admin/")
    async def index():
        return FileResponse(ASSETS / "index.html")

    @app.get("/admin/assets/{filename}")
    async def asset(filename: str):
        if filename not in {"app.js", "style.css"}:
            raise HTTPException(404)
        return FileResponse(ASSETS / filename)

    @app.post("/admin/api/login")
    async def login(req: Request):
        body = await payload(req)
        # Nginx overwrites X-Real-IP; the application port is loopback-only.
        ip = req.headers.get("x-real-ip") or (req.client.host if req.client else "unknown")
        now = time.time()
        with store.lock:
            store.attempts = {k: v for k, v in store.attempts.items() if v[-1] > now - 600}
            failures = [t for t in store.attempts.get(ip, []) if t > now - 600]
            if len(failures) >= 8:
                raise HTTPException(429, "尝试次数过多，请 10 分钟后重试")
            value = body.get("key")
            if not isinstance(value, str) or not hmac.compare_digest(digest(value), store.admin_digest):
                store.attempts[ip] = failures + [now]
                raise HTTPException(401, "管理密钥不正确")
            store.attempts.pop(ip, None)
            store.sessions = {k: v for k, v in store.sessions.items() if v["expires"] > now}
            sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            if len(store.sessions) >= 100:
                store.sessions.pop(next(iter(store.sessions)))
            store.sessions[digest(sid)] = {"csrf": csrf, "expires": now + 12 * 3600}
        response = JSONResponse({"csrf": csrf})
        response.set_cookie(COOKIE, sid, secure=secure_cookie, httponly=True, samesite="strict", path="/admin", max_age=12 * 3600)
        return response

    @app.get("/admin/api/session")
    async def session(req: Request):
        return {"csrf": store.require_admin(req)["csrf"]}

    @app.post("/admin/api/logout")
    async def logout(req: Request):
        store.require_admin(req)
        await browser_login.cancel_owner(digest(req.cookies.get(COOKIE, "")))
        with store.lock:
            store.sessions.pop(digest(req.cookies.get(COOKIE, "")), None)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/admin", secure=secure_cookie, httponly=True, samesite="strict")
        return response

    @app.post("/admin/api/oauth/start")
    async def oauth_start(req: Request):
        store.require_admin(req)
        body = await payload(req)
        name = clean_name(body.get("name"), None)
        try:
            return await browser_login.start(digest(req.cookies.get(COOKIE, "")), name)
        except (httpx.HTTPError, ValueError, TypeError):
            raise HTTPException(502, "授权服务连接失败，请稍后重试")

    @app.post("/admin/api/oauth/{fid}/poll")
    async def oauth_poll(fid: str, req: Request):
        store.require_admin(req)
        try:
            return await browser_login.poll(fid, digest(req.cookies.get(COOKIE, "")))
        except (httpx.HTTPError, ValueError, TypeError):
            raise HTTPException(502, "授权状态暂时无法获取，请稍后重试")

    @app.delete("/admin/api/oauth/{fid}")
    async def oauth_cancel(fid: str, req: Request):
        store.require_admin(req)
        return await browser_login.cancel(fid, digest(req.cookies.get(COOKIE, "")))

    @app.get("/admin/api/overview")
    async def overview(req: Request):
        store.require_admin(req)
        with store.lock:
            keys = [{"id": kid, **{k: v for k, v in item.items() if k != "hash"}} for kid, item in store.data["keys"].items()]
            return {"accounts": pool.rows(store.account_rows()), "pool": dict(store.data["pool"]), "metrics": metrics.snapshot(), "keys": keys, "models": converter.get_available_models(), "uptime": int(time.time() - store.started), "events": list(store.events), "model_cost": pool.cost_table(), "logs": logstore.usage()}

    @app.post("/admin/api/model-cost")
    async def model_cost(req: Request):
        """把实测结果写进站点计费账本（对应网关的 probe 命令）。

        调度器只会从真实响应里学，命中免费站要碰运气；这里允许外部探测脚本
        直接把「某模型在某站免费/收费」灌进来，立即生效。
        """
        store.require_admin(req)
        body = await payload(req)
        incoming = body.get("model_cost")
        if not isinstance(incoming, dict):
            raise HTTPException(400, "model_cost 必须是对象")
        merged, rejected = 0, 0
        with store.lock:
            ledger = store.data.setdefault("model_cost", {})
            for model, sites in list(incoming.items())[:200]:
                if not isinstance(model, str) or not model.strip() or len(model) > 200 or not isinstance(sites, dict):
                    rejected += 1
                    continue
                entry = ledger.setdefault(model.strip(), {})
                for site, verdict in sites.items():
                    if site not in ("cn", "intl") or verdict not in ("free", "paid"):
                        rejected += 1
                        continue
                    if entry.get(site) != verdict:
                        entry[site] = verdict
                        merged += 1
            if merged:
                store.save()
            return {"ok": True, "updated": merged, "rejected": rejected, "model_cost": pool.cost_table()}

    @app.get("/admin/api/logs")
    async def logs(req: Request, limit: int = 100, offset: int = 0,
                   model: str = "", key: str = "", outcome: str = "", account: str = ""):
        """按时间倒序翻请求日志；只含元数据，不含提示词与回复。"""
        store.require_admin(req)
        result = logstore.read(limit=limit, offset=offset,
                               model=model.strip() or None, key=key.strip() or None,
                               outcome=outcome.strip() or None, account=account.strip() or None)
        result["usage"] = logstore.usage()
        return result

    @app.get("/admin/api/usage")
    async def usage(req: Request, start: str = "", end: str = "",
                    account: str = "", key: str = ""):
        """按日期范围聚合用量，供柱状图 / 折线图与排行榜使用。

        只统计成功完成的请求；缺失日期补零，保证图表 X 轴连续。
        """
        store.require_admin(req)
        today = datetime.now(LOG_TZ).date()
        finish = parse_day(end) or today
        begin = parse_day(start) or (finish - timedelta(days=6))
        if begin > finish:
            raise HTTPException(400, "开始日期不能晚于结束日期")
        span = (finish - begin).days + 1
        if span > AGGREGATE_MAX_DAYS:
            raise HTTPException(400, f"时间跨度最多 {AGGREGATE_MAX_DAYS} 天，当前 {span} 天")
        result = logstore.aggregate(begin, finish,
                                    account=account.strip() or None,
                                    key=key.strip() or None)
        result["usage"] = logstore.usage()
        return result

    @app.get("/admin/api/logs/service")
    async def service_logs(req: Request, lines: int = 300, level: str = "", logger: str = ""):
        """读取服务运行日志，解析成结构化记录（最新在前）。"""
        store.require_admin(req)
        wanted = level.strip().upper()
        return logstore.read_service(lines=lines, level=wanted or None,
                                     logger=logger.strip() or None)

    @app.post("/admin/api/accounts/{aid}/actions/{action}")
    async def account_action(aid: str, action: str, req: Request):
        store.require_admin(req)
        if action not in ("refresh", "status", "checkin"):
            raise HTTPException(404)
        return await asyncio.to_thread(pool.operate, aid, action)

    @app.post("/admin/api/pool/actions/{action}")
    async def pool_action(action: str, req: Request):
        store.require_admin(req)
        if action not in ("status", "checkin"):
            raise HTTPException(404)
        return await pool.batch(action)

    @app.patch("/admin/api/pool/settings")
    async def pool_settings(req: Request):
        store.require_admin(req)
        body = await payload(req)
        import re
        with store.lock:
            settings = dict(store.data["pool"])
            if "routing" in body:
                if body["routing"] not in ("manual", "round_robin"):
                    raise HTTPException(400, "调度方式无效")
                settings["routing"] = body["routing"]
            if "auto_checkin" in body:
                if type(body["auto_checkin"]) is not bool:
                    raise HTTPException(400, "签到开关无效")
                settings["auto_checkin"] = body["auto_checkin"]
            if "checkin_time" in body:
                if not isinstance(body["checkin_time"], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", body["checkin_time"]):
                    raise HTTPException(400, "请选择有效的签到时间")
                settings["checkin_time"] = body["checkin_time"]
            if "min_credits" in body:
                value = body["min_credits"]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1_000_000:
                    raise HTTPException(400, "低余额阈值需为 0–1000000 之间的数字")
                settings["min_credits"] = float(value)
            store.data["pool"] = settings
            store.save()
        return {"ok": True, "pool": settings}

    @app.post("/admin/api/accounts")
    async def add_account(req: Request):
        store.require_admin(req)
        body = await payload(req)
        doc = body.get("credential")
        store.validate_credential(doc)
        name = clean_name(body.get("name"), display_name(doc, "新账号"))
        with store.lock:
            if len(store.data["accounts"]) >= 100:
                raise HTTPException(400, "最多保存 100 个账号")
            aid = secrets.token_hex(8)
            filename = aid + ".info"
            write_json(store.auth_dir / filename, doc)
            store.data["accounts"][aid] = {"file": filename, "name": name, "created": int(time.time()), "enabled": True}
            if store.data["active"] is None:
                store.data["active"] = aid
            store.save()
            store.sync_active()
        return {"id": aid}

    @app.patch("/admin/api/accounts/{aid}")
    async def edit_account(aid: str, req: Request):
        store.require_admin(req)
        body = await payload(req)
        with store.lock:
            item = store.data["accounts"].get(aid)
            if not item:
                raise HTTPException(404, "账号不存在")
            new = dict(item)
            if "name" in body:
                new["name"] = clean_name(body["name"], item["name"])
            if "enabled" in body:
                if type(body["enabled"]) is not bool:
                    raise HTTPException(400, "启用状态不正确")
                new["enabled"] = body["enabled"]
            if body.get("active") is True:
                if not new["enabled"]:
                    raise HTTPException(400, "请先启用账号")
                try:
                    store.validate_credential(json.loads(store.file_for(new).read_text(encoding="utf-8")))
                except (ValueError, OSError):
                    raise HTTPException(400, "凭据文件无法读取，请重新导入")
                store.data["active"] = aid
            store.data["accounts"][aid] = new
            if not new["enabled"] and store.data["active"] == aid:
                store.data["active"] = None
            store.save()
            store.sync_active()
        return {"ok": True}

    @app.delete("/admin/api/accounts/{aid}")
    async def delete_account(aid: str, req: Request):
        store.require_admin(req)
        with store.lock:
            item = store.data["accounts"].get(aid)
            if not item:
                raise HTTPException(404, "账号不存在")
            trash = store.root / "trash" / (aid + "-" + secrets.token_hex(4))
            trash.mkdir(parents=True, mode=0o700)
            source = store.file_for(item)
            if source.exists():
                write_json(trash / "credential.json", json.loads(source.read_text(encoding="utf-8")))
            write_json(trash / "metadata.json", item)
            del store.data["accounts"][aid]
            if store.data["active"] == aid:
                store.data["active"] = None
            store.save()
            store.sync_active()
            # Keep the file so an in-flight token refresh may still complete safely.
            # Account storage is authoritative; deleted records cannot be selected.
        return {"ok": True}

    @app.post("/admin/api/keys")
    async def add_key(req: Request):
        store.require_admin(req)
        body = await payload(req)
        name = clean_name(body.get("name"), "新客户端")
        with store.lock:
            if len(store.data["keys"]) >= 100:
                raise HTTPException(400, "最多保存 100 枚 API Key")
            kid, key = store.add_key(name)
            store.save()
        return {"id": kid, "key": key}

    @app.delete("/admin/api/keys/{kid}")
    async def revoke_key(kid: str, req: Request):
        store.require_admin(req)
        with store.lock:
            if kid not in store.data["keys"]:
                raise HTTPException(404, "密钥不存在")
            if len(store.data["keys"]) <= 1:
                raise HTTPException(400, "请先创建一枚新密钥，再撤销最后一枚密钥")
            del store.data["keys"][kid]
            store.save()
        return {"ok": True}

    @app.post("/admin/api/test")
    async def test_model(req: Request):
        store.require_admin(req)
        body = await payload(req)
        model = body.get("model", "deepseek-v4-flash")
        prompt = body.get("prompt", "请只回复：连接成功")

        def active_account():
            """后台测试走的是当前选中账号，日志里也标出来。"""
            with store.lock:
                aid = store.data.get("active")
                item = store.data["accounts"].get(aid) or {}
            return {"name": item.get("name"), "site": pool.site_of(aid)}

        if not isinstance(model, str) or model not in converter.get_available_models():
            raise HTTPException(400, "请选择列表中的模型")
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 2000:
            raise HTTPException(400, "测试消息需为 1–2000 个字符")
        if store.test_lock.locked():
            raise HTTPException(409, "已有测试正在进行")
        if converter.CONFIG.get("cred") is None:
            raise HTTPException(400, "请先导入并启用一个账号")
        async with store.test_lock:
            started = time.monotonic()
            # Use a short-lived API key with the same authentication path as clients.
            with store.lock:
                temp_key = secrets.token_urlsafe(48)
                store.test_keys.add(digest(temp_key))
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=MetricsMiddleware(converter.app, metrics, source="test", key_lookup=store.key_label, account_lookup=active_account)), base_url="http://internal") as client:
                    result = await asyncio.wait_for(client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + temp_key}, json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 1024, "stream": False}), timeout=90)
                data = result.json()
                answer = data.get("choices", [{}])[0].get("message", {}).get("content", "") if result.status_code == 200 else ""
                output = {"ok": result.status_code == 200 and bool(answer), "status": result.status_code, "answer": answer, "seconds": round(time.monotonic() - started, 2), "usage": data.get("usage") if result.status_code == 200 else None}
                if not output["ok"]:
                    output["error"] = "上游未返回有效回复，请检查登录凭据、账号额度或模型权限。"
                    if result.status_code == 200 and data.get("choices", [{}])[0].get("finish_reason") == "length":
                        output["error"] = "已连接上游，但生成预算耗尽，未获得正文。请在客户端提高 max_tokens 后重试。"
            except (Exception, asyncio.TimeoutError):
                output = {"ok": False, "error": "调用失败或超时，请检查凭据是否有效，稍后重试。", "seconds": round(time.monotonic() - started, 2)}
            finally:
                with store.lock:
                    store.test_keys.discard(digest(temp_key))
            store.events.appendleft({"time": int(time.time()), "model": model, "ok": output["ok"], "seconds": output["seconds"]})
            return output

    app.mount("/", converter.app)
    return app


if __name__ == "__main__":
    _app = create_app()
    setup_logging(_app.state.logstore.dir)
    uvicorn.run(_app, host="0.0.0.0", port=8787, log_level="warning",
                proxy_headers=True, forwarded_allow_ips="*", log_config=None)

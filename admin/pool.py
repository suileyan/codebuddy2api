"""Account-scoped billing, refresh, daily check-in and request routing."""
import asyncio
import hashlib
import json
import math
import threading
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from core.converter import INTL_EDITIONS

REQUEST_CREDENTIAL = ContextVar("pool_credential", default=None)
CN = timezone(timedelta(hours=8))
# 计费接口按站点分域名：国内站与国际站的 token 互不通用，跨站调用一律 401。
BILLING_HOSTS = {"cn": "https://www.codebuddy.cn", "intl": "https://www.workbuddy.ai"}
BILLING_PATH = "/v2/billing/meter/"


def billing_host(headers):
    """按凭据的 X-Domain 选择计费域名；缺失或未知时回退国内站。"""
    domain = str(headers.get("X-Domain") or "").lower()
    return BILLING_HOSTS["intl"] if "workbuddy.ai" in domain else BILLING_HOSTS["cn"]


# 站点维度的模型计费账本：{model: {"cn"|"intl": "free"|"paid"}}。
#
# 只信**实测** usage.credit，不信模型目录的 credits 倍率：目录是客户端展示清单，
# 会漏模型（国际站目录里没有 deepseek-v4.1-flash，实测 846 tokens 仍 credit=0 免费），
# 也会过期（国际站目录给 hy4-preview 标 x0.00，实测已经要计费）。
#
# 阈值取 300 而不是更小的值：credit 以 0.01 为步长取整，便宜模型在短请求下会
# 四舍五入成 0。实测 glm-5.3-flash 国际站 187 tokens 记 0、337 tokens 就记 0.01，
# 所以 100 会把「很便宜」误判成「免费」。
MODEL_FREE_MIN_TOKENS = 300

# 余额低于此值时，不再把「已实测收费」的模型派给该账号。
# 动机：余额见底后上游连免费模型都整体拒绝（429 / code=14018），
# 留一点余量让账号还能继续承接免费模型。
MIN_CREDITS_FOR_PAID = 50


def model_site(headers):
    """凭据所属站点标识，与计费域名同源判断。"""
    return "intl" if billing_host(headers) == BILLING_HOSTS["intl"] else "cn"


def number(value):
    if isinstance(value, bool):
        raise ValueError("invalid number")
    try:
        n = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("invalid number")
    if not n.is_finite():
        raise ValueError("invalid number")
    return max(Decimal(0), n)


def summarize_packages(packages):
    remain = Decimal(0)
    size = Decimal(0)
    for p in packages:
        # Precise values carry fractional credit balances; cycle values are spendable now.
        prefix = "Cycle" if any(k in p for k in ["CycleCapacityRemainPrecise", "CycleCapacityRemain"]) else ""
        def field(name, default=None):
            return p.get(prefix + name + "Precise", p.get(prefix + name, default))
        r = number(field("CapacityRemain"))
        capacity = number(field("CapacitySize", r))
        remain += min(r, capacity) if capacity else r
        size += capacity
    return {"remaining": float(remain), "capacity": float(size), "used": float(max(Decimal(0), size - remain)), "packages": len(packages)}


class BillingError(Exception):
    def __init__(self, message, status=502, code=None):
        super().__init__(message)
        self.status, self.code = status, code


class AccountPool:
    def __init__(self, store, clock=time.time, client_factory=None):
        self.store, self.clock = store, clock
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=20, follow_redirects=False))
        self.locks = {}
        self.cursor = 0
        self.jobs = asyncio.Lock()
        self.last_sync = 0
        self._sites = {}
        with store.lock:
            store.data.setdefault("pool", {"routing": "round_robin", "auto_checkin": True,
                                           "checkin_time": "09:00", "min_credits": MIN_CREDITS_FOR_PAID})
            store.data.setdefault("account_status", {})
            store.data.setdefault("session_bindings", {})
            store.data.setdefault("model_cost", {})
            store.save()

    def operation_lock(self, aid):
        with self.store.lock:
            return self.locks.setdefault(aid, threading.Lock())

    def snapshot(self, aid):
        with self.store.lock:
            item = self.store.data["accounts"].get(aid)
            if not item:
                raise HTTPException(404, "账号不存在")
            return dict(item), dict(self.store.data["account_status"].get(aid, {}))

    def update(self, aid, **values):
        with self.store.lock:
            if aid in self.store.data["accounts"]:
                self.store.data["account_status"].setdefault(aid, {}).update(values)
                self.store.save()

    def state(self, row):
        status = self.store.data["account_status"].get(row["id"], {})
        if not row["enabled"]:
            return "paused"
        if row["status"] == "invalid" or status.get("auth_invalid"):
            return "invalid"
        if status.get("cooldown_until", 0) > self.clock():
            return "cooling"
        if status.get("credits_updated", 0) > self.clock() - 600 and status.get("remaining") == 0 and status.get("packages", 0) > 0:
            return "exhausted"
        return "available"

    def rows(self, rows):
        today = datetime.fromtimestamp(self.clock(), CN).strftime("%Y-%m-%d")
        for row in rows:
            status = self.store.data["account_status"].get(row["id"], {})
            row.update({"pool_state": self.state(row), "remaining": status.get("remaining"), "credits_updated": status.get("credits_updated"),
                        "credits_stale": status.get("credits_updated", 0) < self.clock() - 600,
                        "today_checked_in": status.get("checkin_date") == today, "cooldown_until": status.get("cooldown_until", 0),
                        "last_error": status.get("last_error"), "token_refreshed": status.get("token_refreshed"),
                        "request_count": status.get("request_count", 0), "site": self.site_of(row["id"])})
        return rows

    # ------------------------------------------------------------------
    # 站点 / 计费账本
    # ------------------------------------------------------------------

    def site_of(self, aid):
        """账号所属站点（cn / intl）。只读凭据文件、不触发网络刷新，可安全在持锁期间调用。"""
        with self.store.lock:
            item = self.store.data["accounts"].get(aid)
        if not item:
            return None
        key = (item.get("file"), item.get("enabled"))
        cached = self._sites.get(aid)
        if cached and cached[0] == key:
            return cached[1]
        try:
            edition = str(self.store.manager_for(aid, item).edition() or "").strip().lower()
            site = "intl" if edition in INTL_EDITIONS else "cn"
        except Exception:  # noqa: BLE001 - 读不到凭据时按国内站处理，不影响可用性判断
            site = "cn"
        self._sites[aid] = (key, site)
        return site

    def observe_cost(self, aid, model, usage):
        """从一次真实响应学习「该模型在该站点免费还是收费」。

        只有实测 usage.credit 才算证据：credit>0 判收费；credit==0 且样本足够
        （total_tokens ≥ MODEL_FREE_MIN_TOKENS）才判免费 —— 上游对极小请求也会记 0，
        那不是真免费，不能据此把零余额账号送去跑收费模型。
        """
        if not model or not isinstance(usage, dict):
            return
        credit = usage.get("credit")
        if credit is None or isinstance(credit, bool):
            return
        try:
            credit = float(credit)
        except (TypeError, ValueError):
            return
        if credit <= 0:
            total = usage.get("total_tokens")
            if not isinstance(total, (int, float)) or isinstance(total, bool) or total < MODEL_FREE_MIN_TOKENS:
                return
            verdict = "free"
        else:
            verdict = "paid"
        site = self.site_of(aid)
        if not site:
            return
        with self.store.lock:
            entry = self.store.data.setdefault("model_cost", {}).setdefault(model, {})
            if entry.get(site) == verdict:
                return
            entry[site] = verdict
            self.store.save()

    def preferred_free_sites(self, model):
        """该模型「一个站点实测免费、另一个站点实测收费」时，返回应优先使用的站点集合。

        都免费或都收费时返回空集 —— 那种情况下没有可省的钱，照常轮询即可。
        """
        if not model:
            return set()
        with self.store.lock:
            entry = dict((self.store.data.get("model_cost") or {}).get(model) or {})
        free = {site for site, verdict in entry.items() if verdict == "free"}
        paid = {site for site, verdict in entry.items() if verdict == "paid"}
        if not free or not paid:
            return set()
        return free

    def cost_table(self):
        """账本快照，供管理后台展示。"""
        with self.store.lock:
            return {model: dict(sites) for model, sites in (self.store.data.get("model_cost") or {}).items()}

    def cost_verdict(self, site, model):
        """该模型在该站点的实测计费结论：free / paid / None（未知）。"""
        if not site or not model:
            return None
        with self.store.lock:
            entry = (self.store.data.get("model_cost") or {}).get(model) or {}
            return entry.get(site)

    def min_credits(self):
        """低余额保护阈值；0 表示关闭该保护。"""
        with self.store.lock:
            raw = (self.store.data.get("pool") or {}).get("min_credits", MIN_CREDITS_FOR_PAID)
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return float(MIN_CREDITS_FOR_PAID)

    def remaining_of(self, aid):
        with self.store.lock:
            status = self.store.data["account_status"].get(aid) or {}
            return status.get("remaining")

    def account_name(self, aid):
        with self.store.lock:
            item = self.store.data["accounts"].get(aid) or {}
            return item.get("name")

    def can_serve(self, aid, model):
        """余额过低的账号不再承接已实测收费的模型。

        未知模型放行：目录不可信、实测账本又只会慢慢积累，一律拦截会让
        低余额账号几乎无法使用。真正要拦住的是「已经知道要花钱」的请求。
        """
        threshold = self.min_credits()
        if threshold <= 0 or not model:
            return True
        remaining = self.remaining_of(aid)
        if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
            return True  # 余额未知：不拦
        if remaining > threshold:
            return True
        return self.cost_verdict(self.site_of(aid), model) != "paid"

    def select(self, affinity_key=None, model=None):
        # 站点优先级必须在持锁前算好：preferred_free_sites 自己会取 store.lock。
        preferred = self.preferred_free_sites(model)
        with self.store.lock:
            candidates = [row for row in self.store.account_rows() if self.state(row) == "available"]
            if self.store.data["pool"]["routing"] == "manual":
                candidates = [row for row in candidates if row["id"] == self.store.data["active"]]
            if preferred:
                # 只在「免费站还有可用账号」时收窄候选；否则照常轮询，避免无谓失败。
                narrowed = [row for row in candidates if self.site_of(row["id"]) in preferred]
                if narrowed:
                    candidates = narrowed
            if not candidates:
                raise HTTPException(503, "暂无可用账号：请检查暂停、积分、冷却或登录状态")
            # 低余额保护：余额 ≤ 阈值的账号不再接已实测收费的模型。
            if model and self.min_credits() > 0:
                affordable = [row for row in candidates if self.can_serve(row["id"], model)]
                if not affordable:
                    known = [self.remaining_of(row["id"]) for row in candidates]
                    known = [value for value in known if isinstance(value, (int, float)) and not isinstance(value, bool)]
                    lowest = f"，最低 {min(known):g}" if known else ""
                    raise HTTPException(
                        503, f"模型 {model} 在可用账号上都属于付费模型，而这些账号余额均不超过 "
                             f"{self.min_credits():g}{lowest}。请补充积分、调整阈值，或改用免费模型。")
                candidates = affordable
            now = self.clock()
            bindings = self.store.data["session_bindings"]
            for key in list(bindings):
                if bindings[key].get("expires", 0) <= now:
                    del bindings[key]
            bound = bindings.get(affinity_key, {}).get("account_id")
            if bound in {row["id"] for row in candidates}:
                aid = bound
            else:
                aid = candidates[self.cursor % len(candidates)]["id"]
                self.cursor += 1
            if affinity_key:
                if affinity_key not in bindings and len(bindings) >= 4096:
                    del bindings[min(bindings, key=lambda key: bindings[key]["expires"])]
                bindings[affinity_key] = {"account_id": aid, "expires": now + 86400}
            item = self.store.data["accounts"][aid]
            return aid, self.store.manager_for(aid, item)

    def record(self, aid, status=200):
        with self.store.lock:
            if aid not in self.store.data["accounts"]:
                return
            data = self.store.data["account_status"].setdefault(aid, {})
            data["request_count"] = data.get("request_count", 0) + 1
            data["last_request"] = int(self.clock())
            if status in (401, 403):
                data.update(cooldown_until=self.clock() + 300, last_error="上游认证或访问被拒绝，已冷却 5 分钟")
            elif status in (402, 429):
                data.update(cooldown_until=self.clock() + 1800, last_error="上游额度或频率限制，已冷却 30 分钟")
            self.store.save()

    def billing(self, client, headers, path, body=None):
        response = client.post(billing_host(headers) + BILLING_PATH + path, headers=headers, json=body or {})
        if response.status_code != 200:
            raise BillingError("积分服务请求失败", response.status_code)
        try:
            d = response.json()
        except ValueError:
            raise BillingError("积分服务返回格式异常")
        if not isinstance(d, dict) or d.get("code") != 0:
            raise BillingError("上游未接受该操作，请检查账号或稍后重试", code=d.get("code") if isinstance(d, dict) else None)
        return d.get("data")

    def credits(self, client, headers):
        packages = []
        now = datetime.fromtimestamp(self.clock(), CN)
        for page in range(1, 101):
            d = self.billing(client, headers, "get-user-resource", {"PageNumber": page, "PageSize": 100, "ProductCode": "p_tcaca", "Status": [0, 3], "PackageEndTimeRangeBegin": now.strftime("%Y-%m-%d %H:%M:%S"), "PackageEndTimeRangeEnd": "2126-01-01 00:00:00"})
            try:
                data = d["Response"]["Data"]
                current = data["Accounts"]
                total = int(data["TotalCount"])
                if not isinstance(current, list) or total < 0:
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise BillingError("积分数据格式异常，上次余额已保留")
            packages.extend(current)
            if len(packages) >= total:
                return summarize_packages(packages)
            if not current:
                break
        raise BillingError("积分包分页不完整，上次余额已保留")

    def checkin_status(self, client, headers):
        try:
            d = self.billing(client, headers, "checkin-activity-status")
        except BillingError as e:
            if e.status not in (404, 405):
                raise
            d = self.billing(client, headers, "checkin-status")
        if not isinstance(d, dict):
            raise BillingError("签到状态格式异常")
        def flag(snake, camel):
            v = d.get(snake, d.get(camel))
            if v not in (True, False, 0, 1, "true", "false", "1", "0"):
                raise BillingError("签到状态缺少必要字段")
            return v in (True, 1, "true", "1")
        return {"active": flag("active", "active"), "checked": flag("today_checked_in", "todayCheckedIn")}

    def operate(self, aid, action):
        with self.operation_lock(aid):
            item, previous = self.snapshot(aid)
            if not item["enabled"]:
                return {"id": aid, "ok": False, "message": "账号已暂停，未执行操作"}
            manager = self.store.manager_for(aid, item)
            try:
                if action == "refresh":
                    with manager._lock:
                        manager._refresh()
                    self.update(aid, token_refreshed=int(self.clock()), auth_invalid=False, cooldown_until=0, last_error=None)
                    return {"id": aid, "ok": True, "message": "登录凭据已刷新"}
                headers = manager.get_headers()
                today = datetime.fromtimestamp(self.clock(), CN).strftime("%Y-%m-%d")
                with self.client_factory() as client:
                    message = "积分状态已更新"
                    checkin_error = None
                    try:
                        ci = self.checkin_status(client, headers)
                        if ci["checked"]:
                            self.update(aid, checkin_date=today)
                        if action == "checkin":
                            if ci["checked"]:
                                message = "今日已签到，无需重复签到"
                            elif not ci["active"]:
                                message = "当前没有可参与的签到活动"
                            else:
                                self.billing(client, headers, "daily-checkin")
                                ci = self.checkin_status(client, headers)
                                if not ci["checked"]:
                                    raise BillingError("签到已提交，但上游尚未确认；请刷新状态")
                                self.update(aid, checkin_date=today)
                                message = "签到成功"
                    except (BillingError, httpx.HTTPError) as e:
                        if action == "checkin":
                            raise
                        checkin_error = "签到状态暂时无法查询"
                    data = self.credits(client, headers)
                    # A successful balance lookup is proof of current authentication.
                    self.update(aid, **data, credits_updated=int(self.clock()), auth_invalid=False, last_error=checkin_error)
                    if data["remaining"] > 0 and action == "checkin":
                        self.update(aid, cooldown_until=0)
                    return {"id": aid, "ok": True, "message": message, "remaining": data["remaining"]}
            except BillingError as e:
                self.update(aid, last_error=str(e))
                if e.status in (401, 403):
                    self.update(aid, cooldown_until=self.clock()+300)
                return {"id": aid, "ok": False, "message": str(e)}
            except (httpx.HTTPError, OSError, ValueError, RuntimeError, KeyError):
                # Do not expose raw refresh responses or bearer credentials.
                message = "操作失败：请检查登录凭据或网络，必要时重新授权"
                self.update(aid, last_error=message)
                return {"id": aid, "ok": False, "message": message}

    async def batch(self, action):
        if self.jobs.locked():
            raise HTTPException(409, "已有批量任务进行中，请稍后刷新")
        async with self.jobs:
            with self.store.lock:
                ids = [aid for aid, item in self.store.data["accounts"].items() if item["enabled"]]
            semaphore = asyncio.Semaphore(3)
            async def run(aid):
                async with semaphore:
                    try:
                        return await asyncio.to_thread(self.operate, aid, action)
                    except HTTPException:
                        return {"id": aid, "ok": False, "message": "账号已移除"}
            return {"results": await asyncio.gather(*(run(aid) for aid in ids))}

    async def tick(self):
        now = datetime.fromtimestamp(self.clock(), CN)
        with self.store.lock:
            cfg = dict(self.store.data["pool"])
            due = [aid for aid, item in self.store.data["accounts"].items() if item["enabled"] and self.store.data["account_status"].get(aid, {}).get("checkin_date") != now.strftime("%Y-%m-%d") and self.store.data["account_status"].get(aid, {}).get("auto_attempt", 0) < self.clock()-1800]
        if cfg["auto_checkin"] and now.strftime("%H:%M") >= cfg["checkin_time"]:
            for aid in due:
                self.update(aid, auto_attempt=int(self.clock()))
                try:
                    await asyncio.to_thread(self.operate, aid, "checkin")
                except HTTPException:
                    pass
        if self.clock() - self.last_sync > 300 and not self.jobs.locked():
            await self.batch("status")
            self.last_sync = self.clock()


def request_affinity(headers, body):
    """Persist only a hash of caller/model/session identity, never raw prompts or keys."""
    if not isinstance(body, dict):
        return None
    identity = None
    for name in (b"x-session-id", b"session_id", b"x-conversation-id"):
        if headers.get(name):
            identity = [name.decode(), headers[name].decode(errors="replace")]
            break
    if identity is None:
        for name in ("prompt_cache_key", "conversation_id", "session_id"):
            if body.get(name):
                identity = [name, body[name]]
                break
    if identity is None:
        messages = body.get("messages", body.get("input", []))
        if isinstance(messages, str) and messages:
            identity = ["first_user", messages]
        elif isinstance(messages, list):
            for message in messages:
                if isinstance(message, dict) and message.get("role") == "user":
                    identity = ["first_user", message.get("content", "")]
                    break
    if identity is None:
        return None
    caller = headers.get(b"authorization") or headers.get(b"x-api-key", b"")
    value = [caller.decode(errors="replace"), body.get("model", "auto"), identity]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class PoolMiddleware:
    def __init__(self, app, pool):
        self.app, self.pool = app, pool

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] not in {"/v1/chat/completions", "/v1/responses", "/v1/messages", "/v1/messages/count_tokens"}:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            self.pool.store.check_api(headers.get(b"authorization", b"").decode(), headers.get(b"x-api-key", b"").decode())
            raw = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                raw.extend(message.get("body", b""))
                if len(raw) > 32 * 1024 * 1024:
                    raise HTTPException(413, "请求体超过 32 MiB")
                if not message.get("more_body", False):
                    break
            raw = bytes(raw)
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                body = {}
            affinity_key = request_affinity(headers, body)
            model = body.get("model")
            model = model.strip() if isinstance(model, str) and model.strip() else None
            if model == "auto":
                model = None
            original_receive = receive
            delivered = False
            async def replay_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": raw, "more_body": False}
                return await original_receive()
            receive = replay_receive
            aid, manager = self.pool.select(affinity_key, model)
            # Refresh before entering a stream; retry another eligible account only
            # if credential preparation fails, never replay a partially emitted response.
            try:
                await asyncio.to_thread(manager.get_headers)
            except Exception:
                self.pool.update(aid, cooldown_until=self.pool.clock()+300, last_error="凭据暂不可用，已冷却 5 分钟")
                aid, manager = self.pool.select(affinity_key, model)
                await asyncio.to_thread(manager.get_headers)
        except HTTPException as e:
            return await JSONResponse({"detail": e.detail}, e.status_code)(scope, receive, send)
        except Exception:
            return await JSONResponse({"detail": "账号凭据暂不可用，请刷新凭据或重新授权"}, 503)(scope, receive, send)
        token = REQUEST_CREDENTIAL.set(manager)
        # 把实际服务的账号挂到 scope["state"]，外层的指标中间件据此记录到请求日志。
        try:
            state = scope.setdefault("state", {})
            if isinstance(state, dict):
                state["account"] = {"id": aid, "name": self.pool.account_name(aid),
                                    "site": self.pool.site_of(aid)}
        except Exception:  # noqa: BLE001 - 记录失败不影响请求
            pass
        status = 200
        buffer = b""
        async def observe(message):
            nonlocal status, buffer
            if message["type"] == "http.response.start":
                status = message["status"]
            if message["type"] == "http.response.body":
                buffer += message.get("body", b"")
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line.startswith(b"data:"):
                        try:
                            obj = json.loads(line[5:])
                            usage = obj.get("usage")
                            if isinstance(usage, dict):
                                self.pool.observe_cost(aid, model, usage)
                            err = obj.get("error") or (obj.get("response") or {}).get("error") or {}
                            code = err.get("code") if isinstance(err, dict) else None
                            if isinstance(code, str) and code.isdigit():
                                code = int(code)
                            if code in (401, 402, 403, 429):
                                status = code
                        except (ValueError, AttributeError):
                            pass
                if len(buffer) > 65536:
                    buffer = b""
            await send(message)
        try:
            await self.app(scope, receive, observe)
        finally:
            REQUEST_CREDENTIAL.reset(token)
            self.pool.record(aid, status)

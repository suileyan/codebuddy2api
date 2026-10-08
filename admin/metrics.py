"""Bounded process-lifetime counters; never store prompts, responses or raw keys."""
import json
import threading
import time
from collections import deque

PATHS = {"/v1/chat/completions", "/v1/responses", "/v1/messages"}

# 请求体只缓存前 256 KB，仅用于取 model 字段
MAX_REQUEST_BYTES = 262144


def _count(value):
    """token 计数：只接受非负整数，其余（含 bool / 浮点 / 缺失）一律 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return int(value)


class RequestMetrics:
    def __init__(self, sink=None):
        self.lock = threading.Lock()
        self.started_at = int(time.time())
        self.in_flight = self.total = self.success = self.http_success = 0
        self.duration_sum = 0
        self.api_count = self.test_count = 0
        self.recent = deque(maxlen=100)
        # sink(record)：把每条完成的请求落到磁盘。可选，失败绝不影响请求。
        self.sink = sink

    def begin(self):
        with self.lock:
            self.in_flight += 1

    def restore(self, records):
        """启动时用磁盘上的历史回填「最近请求」，让界面重启后不清零。

        只回填展示用的 recent，不碰累计计数器 —— 那些语义是「本次运行以来」。
        """
        with self.lock:
            self.recent.clear()
            # 先滤掉坏行再截断，否则日志里混入损坏行会让回填条数不足。
            valid = [r for r in records if isinstance(r, dict)]
            for record in valid[:self.recent.maxlen]:
                self.recent.append(record)

    def finish(self, path, source, status, ok, duration, outcome,
               key=None, model=None, credits=None, account=None, site=None,
               tokens=None, prompt_tokens=None, completion_tokens=None):
        record = {"time": int(time.time()), "path": path, "source": source,
                  "status": status, "ok": ok, "duration_ms": round(duration),
                  "outcome": outcome, "key": key, "model": model, "credits": credits,
                  "account": account, "site": site, "tokens": tokens,
                  "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
        with self.lock:
            self.in_flight -= 1
            self.total += 1
            self.success += int(ok)
            self.http_success += int(status is not None and 200 <= status < 300)
            self.duration_sum += duration
            self.api_count += int(source == "api")
            self.test_count += int(source == "test")
            self.recent.appendleft(record)
        if self.sink is not None:
            try:
                self.sink(record)
            except Exception:  # noqa: BLE001 - 落盘失败不能影响请求
                pass

    def snapshot(self):
        with self.lock:
            return {"started_at": self.started_at, "completed": self.total, "in_flight": self.in_flight,
                    "succeeded": self.success, "failed": self.total - self.success,
                    "success_rate": round(100*self.success/self.total, 1) if self.total else None,
                    "http_success_rate": round(100*self.http_success/self.total, 1) if self.total else None,
                    "avg_duration_ms": round(self.duration_sum/self.total) if self.total else None,
                    "api_count": self.api_count, "test_count": self.test_count, "recent": list(self.recent)}


class MetricsMiddleware:
    def __init__(self, app, metrics, source="api", key_lookup=None, account_lookup=None):
        self.app, self.metrics, self.source = app, metrics, source
        # key_lookup: callable(token) -> 便于识别的密钥标签（如「名称 · sk-wb…Q0」）
        self.key_lookup = key_lookup
        # account_lookup: callable() -> {"name", "site"}；用于没走账号池的通道（如后台测试）
        self.account_lookup = account_lookup

    def _key_label(self, scope):
        """从请求头解析客户端密钥，映射成可读标签；未识别时返回 None。"""
        if self.key_lookup is None:
            return None
        authorization = x_api_key = ""
        for name, value in scope.get("headers") or []:
            lowered = name.lower()
            if lowered == b"authorization":
                authorization = value.decode("latin-1")
            elif lowered == b"x-api-key":
                x_api_key = value.decode("latin-1")
        token = authorization[7:].strip() if authorization[:7].lower() == "bearer " else x_api_key
        if not token:
            return None
        try:
            return self.key_lookup(token)
        except Exception:
            return None  # 统计失败绝不影响请求本身

    @staticmethod
    def _request_model(buffer):
        """从缓存的请求体里取 model 字段；取不到返回 None。"""
        if not buffer:
            return None
        try:
            body = json.loads(buffer)
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        model = body.get("model")
        if isinstance(model, str) and model:
            return model
        return "auto"  # 与转换器的默认值保持一致

    async def __call__(self, scope, receive, send):
        path = scope.get("path")
        if scope["type"] != "http" or scope["method"] != "POST" or path not in PATHS:
            return await self.app(scope, receive, send)
        start = time.monotonic()
        self.metrics.begin()
        status = None
        completed = failed = streaming = terminal = disconnected = False
        buffer = b""
        oversized_line = False
        request_buffer = bytearray()
        request_done = False
        key_label = self._key_label(scope)

        def event(line):
            nonlocal failed, terminal
            if not line.startswith(b"data:"):
                return
            payload = line[5:].strip()
            if payload == b"[DONE]":
                terminal = True
                return
            try:
                value = json.loads(payload)
            except ValueError:
                return
            if not isinstance(value, dict):
                return
            typ = value.get("type")
            if value.get("error") or typ in ("error", "response.failed", "response.incomplete"):
                failed = True
            if typ in ("response.completed", "message_stop"):
                terminal = True
            response = value.get("response")
            if isinstance(response, dict) and (response.get("error") or response.get("status") in ("failed", "incomplete")):
                failed = True

        async def observed_receive():
            nonlocal disconnected, request_done
            message = await receive()
            if message["type"] == "http.disconnect" and not completed:
                disconnected = True
            elif message["type"] == "http.request" and not request_done:
                chunk = message.get("body", b"")
                if len(request_buffer) + len(chunk) <= MAX_REQUEST_BYTES:
                    request_buffer.extend(chunk)
                else:
                    request_buffer.clear()  # 请求体过大：放弃取 model，不额外占用内存
                    request_done = True
                if not message.get("more_body", False):
                    request_done = True
            return message

        async def observed_send(message):
            nonlocal status, streaming, completed, buffer, failed, oversized_line
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = dict(message.get("headers", []))
                streaming = b"text/event-stream" in headers.get(b"content-type", b"").lower()
            elif message["type"] == "http.response.body":
                if streaming:
                    # Bound storage even if the upstream sends a huge unterminated line.
                    for fragment in message.get("body", b"").splitlines(keepends=True):
                        end = fragment.endswith(b"\n")
                        if not oversized_line and len(buffer) + len(fragment) <= 65536:
                            buffer += fragment
                        else:
                            oversized_line = True
                            buffer = b""
                        if end:
                            if not oversized_line:
                                event(buffer)
                            buffer = b""
                            oversized_line = False
                if not message.get("more_body", False):
                    if streaming and buffer:
                        event(buffer)
                    await send(message)
                    completed = True
                    return
            await send(message)

        try:
            await self.app(scope, observed_receive, observed_send)
        except BaseException:
            failed = True
            raise
        finally:
            http_ok = status is not None and 200 <= status < 300
            ok = http_ok and completed and not disconnected and not failed and (not streaming or terminal)
            outcome = "success" if ok else "stream_error" if failed and streaming else "interrupted" if not completed or disconnected or (streaming and not terminal) else "http_error"
            usage = (scope.get("state") or {}).get("usage") or {}
            credits = usage.get("credit")
            if isinstance(credits, bool) or not isinstance(credits, (int, float)):
                credits = None
            # 实际服务的账号由 PoolMiddleware 写进同一个 scope["state"]。
            served = (scope.get("state") or {}).get("account")
            if not isinstance(served, dict) and self.account_lookup is not None:
                try:
                    served = self.account_lookup()
                except Exception:  # noqa: BLE001 - 取不到账号不影响统计
                    served = None
            served = served if isinstance(served, dict) else {}
            account = served.get("name") if isinstance(served.get("name"), str) else None
            site = served.get("site") if served.get("site") in ("cn", "intl") else None
            self.metrics.finish(path, self.source, status, ok, (time.monotonic()-start)*1000, outcome,
                                key=key_label, model=self._request_model(request_buffer), credits=credits,
                                account=account, site=site,
                                tokens=_count(usage.get("total_tokens")),
                                prompt_tokens=_count(usage.get("prompt_tokens")),
                                completion_tokens=_count(usage.get("completion_tokens")))

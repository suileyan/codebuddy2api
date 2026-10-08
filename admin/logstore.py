"""请求日志与服务运行日志的落盘与回读。

设计取舍：
- 请求日志按天分文件（`requests-YYYY-MM-DD.jsonl`），只记元数据
  （时间/接口/来源/状态/耗时/结果/密钥标签/模型/积分），**绝不落盘提示词与回复**。
- 服务日志按天轮转，`backupCount=0` 即永不自动删除；磁盘占用由使用者在界面上
  看到后自行清理。
- 回读时只从每个文件尾部读有限字节，避免日志变大后拖慢接口。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

CN = timezone(timedelta(hours=8))

# 回读时每个日文件最多扫描的字节数；单日日志远超此值时只覆盖最近部分。
TAIL_SCAN_BYTES = 4 * 1024 * 1024
# 服务日志预览最多返回的条数与读取字节数。
SERVICE_TAIL_LINES = 500
SERVICE_TAIL_BYTES = 512 * 1024

FILE_HANDLER_NAME = "workbuddy2api-service-log"

# 与 setup_logging 里的 Formatter 保持一致：时间 级别 logger 正文
SERVICE_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+([A-Z]+)\s+([\w.]+)\s+(.*)$")
SERVICE_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# 聚合统计：单日文件最多读取的字节数（超出时只取最近部分）。
AGGREGATE_MAX_BYTES = 32 * 1024 * 1024
# 聚合统计允许的最大天数跨度，避免一次拉太多把响应撑大。
AGGREGATE_MAX_DAYS = 90


def parse_day(value):
    """把 'YYYY-MM-DD' 解析成 date；非法返回 None。"""
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def day_range(start, end):
    """闭区间内的所有日期，按时间升序。"""
    days, current = [], start
    while current <= end:
        days.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return days


def _positive_float(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if value > 0 else 0.0


def _positive_int(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value) if value > 0 else 0


def _tail_bytes(path: Path, limit: int) -> bytes:
    """读取文件末尾最多 limit 字节，并丢弃开头可能被截断的半行。"""
    try:
        size = path.stat().st_size
    except OSError:
        return b""
    with open(path, "rb") as f:
        if size > limit:
            f.seek(size - limit)
            chunk = f.read()
            _, _, rest = chunk.partition(b"\n")
            return rest
        return f.read()


def parse_service_lines(lines) -> list[dict]:
    """把服务日志行解析成 {time, level, logger, message, detail}。

    不符合格式的行（堆栈、多行正文）并入上一条的 detail；开头就没有归属的
    裸行单独成条，不至于被丢掉。
    """
    entries: list[dict] = []
    for line in lines:
        matched = SERVICE_LINE.match(line)
        if matched:
            entries.append({"time": matched.group(1), "level": matched.group(2),
                            "logger": matched.group(3), "message": matched.group(4),
                            "detail": ""})
        elif entries:
            previous = entries[-1]
            previous["detail"] = f"{previous['detail']}\n{line}" if previous["detail"] else line
        elif line.strip():
            entries.append({"time": "", "level": "", "logger": "", "message": line, "detail": ""})
    return entries


class LogStore:
    """管理 `logs/` 目录下的请求日志与服务日志。"""

    def __init__(self, root):
        self.dir = Path(root) / "logs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 请求日志
    # ------------------------------------------------------------------

    def request_file(self, day: str) -> Path:
        return self.dir / f"requests-{day}.jsonl"

    def days(self) -> list[str]:
        found = []
        for path in self.dir.glob("requests-*.jsonl"):
            found.append(path.name[len("requests-"):-len(".jsonl")])
        return sorted(found, reverse=True)

    def append(self, record: dict) -> None:
        """追加一条请求记录。调用方已在锁外，这里只做一次短写。"""
        try:
            day = datetime.fromtimestamp(int(record["time"]), CN).strftime("%Y-%m-%d")
        except (KeyError, TypeError, ValueError, OSError):
            return
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            try:
                with open(self.request_file(day), "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass  # 日志写失败绝不影响请求

    def read(self, limit=100, offset=0, model=None, key=None, outcome=None, account=None):
        """按时间倒序分页读取，支持按模型 / 密钥标签 / 账号 / 结果筛选。"""
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        records, skipped, has_more = [], 0, False
        for day in self.days():
            raw = _tail_bytes(self.request_file(day), TAIL_SCAN_BYTES)
            for line in reversed(raw.decode("utf-8", "replace").splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                if model and record.get("model") != model:
                    continue
                if key and record.get("key") != key:
                    continue
                if account and record.get("account") != account:
                    continue
                if outcome and record.get("outcome") != outcome:
                    continue
                if skipped < offset:
                    skipped += 1
                    continue
                if len(records) >= limit:
                    has_more = True
                    break
                records.append(record)
            if has_more:
                break
        return {"records": records, "has_more": has_more, "days": self.days()}

    # ------------------------------------------------------------------
    # 用量聚合
    # ------------------------------------------------------------------

    def _day_records(self, day):
        """逐条产出某天的请求记录；文件损坏或超大都只跳过、不抛错。"""
        path = self.request_file(day)
        try:
            size = path.stat().st_size
        except OSError:
            return
        try:
            with open(path, "rb") as handle:
                if size > AGGREGATE_MAX_BYTES:
                    handle.seek(size - AGGREGATE_MAX_BYTES)
                    chunk = handle.read()
                    _, _, chunk = chunk.partition(b"\n")  # 丢掉被截断的半行
                else:
                    chunk = handle.read()
        except OSError:
            return
        for line in chunk.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record

    def aggregate(self, start, end, account=None, key=None):
        """按日期范围聚合用量，供图表与排行榜使用。

        只统计成功完成的请求（ok 为真）—— 失败请求没有可信的计费与 token 数据。
        缺失的日期会补零，保证图表 X 轴连续。
        """
        days = day_range(start, end)
        daily = {day: {"date": day, "requests": 0, "credits": 0.0, "tokens": 0} for day in days}
        groups = {"models": {}, "accounts": {}, "keys": {}}
        series = {}  # model -> {"credits": {day: n}, "tokens": {day: n}}
        totals = {"requests": 0, "credits": 0.0, "tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}

        for day in days:
            for record in self._day_records(day):
                if not record.get("ok"):
                    continue
                if account and record.get("account") != account:
                    continue
                if key and record.get("key") != key:
                    continue
                credits = _positive_float(record.get("credits"))
                tokens = _positive_int(record.get("tokens"))
                bucket = daily[day]
                bucket["requests"] += 1
                bucket["credits"] += credits
                bucket["tokens"] += tokens
                totals["requests"] += 1
                totals["credits"] += credits
                totals["tokens"] += tokens
                totals["prompt_tokens"] += _positive_int(record.get("prompt_tokens"))
                totals["completion_tokens"] += _positive_int(record.get("completion_tokens"))
                for group, raw in (("models", record.get("model")),
                                   ("accounts", record.get("account")),
                                   ("keys", record.get("key"))):
                    label = raw if isinstance(raw, str) and raw.strip() else "未记录"
                    entry = groups[group].setdefault(label, {"name": label, "requests": 0, "credits": 0.0, "tokens": 0})
                    entry["requests"] += 1
                    entry["credits"] += credits
                    entry["tokens"] += tokens
                if isinstance(record.get("model"), str) and record["model"].strip():
                    slot = series.setdefault(record["model"], {"credits": {}, "tokens": {}})
                    slot["credits"][day] = slot["credits"].get(day, 0.0) + credits
                    slot["tokens"][day] = slot["tokens"].get(day, 0) + tokens

        def ranked(group):
            rows = sorted(groups[group].values(), key=lambda item: (-item["credits"], -item["tokens"], item["name"]))
            for row in rows:
                row["credits"] = round(row["credits"], 4)
            return rows

        return {
            "range": {"from": days[0], "to": days[-1], "days": len(days)},
            "daily": [{**daily[day], "credits": round(daily[day]["credits"], 4)} for day in days],
            "models": ranked("models"),
            "accounts": ranked("accounts"),
            "keys": ranked("keys"),
            "series": [
                {"model": model,
                 "credits": [round(slot["credits"].get(day, 0.0), 4) for day in days],
                 "tokens": [slot["tokens"].get(day, 0) for day in days]}
                for model, slot in sorted(series.items(),
                                          key=lambda item: (-sum(item[1]["credits"].values()),
                                                            -sum(item[1]["tokens"].values()), item[0]))
            ],
            "totals": {**totals, "credits": round(totals["credits"], 4)},
        }

    # ------------------------------------------------------------------
    # 服务运行日志
    # ------------------------------------------------------------------

    def service_files(self) -> list[dict]:
        rows = []
        for path in sorted(self.dir.glob("service.log*"), reverse=True):
            try:
                stat = path.stat()
            except OSError:
                continue
            rows.append({"name": path.name, "size": stat.st_size,
                         "modified": int(stat.st_mtime)})
        return rows

    def read_service(self, lines=SERVICE_TAIL_LINES, level=None, logger=None):
        """读取最新服务日志，解析成结构化记录（最新在前）。

        续行（异常堆栈、多行正文）会并入上一条的 detail，避免界面上出现
        一堆没有归属的裸行。level / logger 用于筛选，counts 始终基于全量，
        这样筛选后仍能看到整体分布。
        """
        lines = max(1, min(int(lines), 5000))
        files = self.service_files()
        if not files:
            return {"entries": [], "counts": {}, "loggers": [], "files": [], "dir": str(self.dir)}
        raw = _tail_bytes(self.dir / files[0]["name"], SERVICE_TAIL_BYTES)
        entries = parse_service_lines(raw.decode("utf-8", "replace").splitlines())
        counts: dict[str, int] = {}
        loggers: dict[str, int] = {}
        for entry in entries:
            counts[entry["level"] or "OTHER"] = counts.get(entry["level"] or "OTHER", 0) + 1
            if entry["logger"]:
                loggers[entry["logger"]] = loggers.get(entry["logger"], 0) + 1
        if level:
            entries = [entry for entry in entries if entry["level"] == level]
        if logger:
            entries = [entry for entry in entries if entry["logger"] == logger]
        ordered = sorted(loggers.items(), key=lambda item: (-item[1], item[0]))
        return {"entries": list(reversed(entries[-lines:])), "counts": counts,
                "loggers": [{"name": name, "count": count} for name, count in ordered],
                "files": files, "dir": str(self.dir)}

    def usage(self) -> dict:
        """日志目录占用，供界面提示使用者自行清理。"""
        total = 0
        count = 0
        for path in self.dir.glob("*"):
            if not path.is_file():
                continue
            try:
                total += path.stat().st_size
            except OSError:
                continue
            count += 1
        return {"dir": str(self.dir), "files": count, "bytes": total,
                "request_days": len(self.days()), "service_files": len(self.service_files())}


def setup_logging(log_dir: Path, level=logging.INFO) -> None:
    """把服务运行日志同时写到控制台与按天轮转的文件。

    幂等：重复调用只会替换自己上一次装的 handler，不会叠加。
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "name", "") == FILE_HANDLER_NAME]:
        root.removeHandler(handler)
        handler.close()
    handler = TimedRotatingFileHandler(log_dir / "service.log", when="midnight",
                                       backupCount=0, encoding="utf-8", delay=True)
    handler.name = FILE_HANDLER_NAME
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    handler.setLevel(level)
    root.addHandler(handler)
    if root.level > level or root.level == logging.NOTSET:
        root.setLevel(level)

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

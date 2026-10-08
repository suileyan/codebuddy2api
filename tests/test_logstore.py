import json
import tempfile
import unittest
from pathlib import Path

from admin.logstore import LogStore, setup_logging, parse_service_lines, _tail_bytes
from admin.metrics import RequestMetrics


def record(index, **overrides):
    base = {"time": 1791000000 + index, "path": "/v1/chat/completions", "source": "api",
            "status": 200, "ok": True, "duration_ms": index, "outcome": "success",
            "key": "codex · sk-wb…a_Q0", "model": "hy3", "credits": 0}
    base.update(overrides)
    return base


class LogStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = LogStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_read_order_and_pagination(self):
        for index in range(250):
            self.logs.append(record(index))
        first = self.logs.read(limit=100)
        self.assertEqual(len(first["records"]), 100)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["records"][0]["duration_ms"], 249)  # 最新在前
        self.assertEqual(first["records"][-1]["duration_ms"], 150)
        second = self.logs.read(limit=100, offset=100)
        self.assertEqual(second["records"][0]["duration_ms"], 149)
        last = self.logs.read(limit=100, offset=200)
        self.assertEqual(len(last["records"]), 50)
        self.assertFalse(last["has_more"])

    def test_filters_by_model_key_and_outcome(self):
        self.logs.append(record(0, model="hy3"))
        self.logs.append(record(1, model="glm-5.3"))
        self.logs.append(record(2, model="glm-5.3", key="other", outcome="stream_error"))
        self.assertEqual([r["duration_ms"] for r in self.logs.read(model="glm-5.3")["records"]], [2, 1])
        self.assertEqual([r["duration_ms"] for r in self.logs.read(key="other")["records"]], [2])
        self.assertEqual([r["duration_ms"] for r in self.logs.read(outcome="success")["records"]], [1, 0])
        self.assertEqual(self.logs.read(model="never")["records"], [])
        # 筛选后 offset 按命中条数计
        page = self.logs.read(model="glm-5.3", offset=1)
        self.assertEqual([r["duration_ms"] for r in page["records"]], [1])

    def test_filters_by_account(self):
        self.logs.append(record(0, account="jywsdww@qq.com", site="intl"))
        self.logs.append(record(1, account="suileyan", site="cn"))
        hits = self.logs.read(account="jywsdww@qq.com")["records"]
        self.assertEqual([r["duration_ms"] for r in hits], [0])
        self.assertEqual(hits[0]["site"], "intl")
        self.assertEqual(self.logs.read(account="nobody")["records"], [])

    def test_corrupt_lines_and_blank_lines_are_skipped(self):
        path = self.logs.request_file("2026-10-08")
        path.write_text("\n".join([
            json.dumps(record(0)),
            "{ not json",
            "",
            json.dumps(record(1)),
            "[1,2,3]",  # 合法 JSON 但不是对象
        ]) + "\n", encoding="utf-8")
        records = self.logs.read()["records"]
        self.assertEqual([r["duration_ms"] for r in records], [1, 0])

    def test_missing_time_is_not_written(self):
        self.logs.append({"path": "/v1/chat/completions"})
        self.assertEqual(self.logs.days(), [])

    def test_usage_reports_dir_and_counts(self):
        self.logs.append(record(0))
        usage = self.logs.usage()
        self.assertEqual(usage["request_days"], 1)
        self.assertEqual(usage["files"], 1)
        self.assertGreater(usage["bytes"], 0)
        self.assertEqual(usage["dir"], str(self.logs.dir))

    def test_tail_bytes_drops_partial_first_line(self):
        path = Path(self.tmp.name) / "sample.txt"
        path.write_bytes(b"".join(b"line-%05d\n" % i for i in range(2000)))
        chunk = _tail_bytes(path, 100)
        self.assertTrue(chunk.startswith(b"line-"))
        self.assertTrue(chunk.endswith(b"\n"))
        self.assertLess(len(chunk), 100)

    def test_service_log_setup_is_idempotent_and_tail_readable(self):
        import logging
        setup_logging(self.logs.dir)
        setup_logging(self.logs.dir)  # 重复调用不应叠加 handler
        handlers = [h for h in logging.getLogger().handlers
                    if getattr(h, "name", "") == "workbuddy2api-service-log"]
        self.assertEqual(len(handlers), 1)
        logging.getLogger("workbuddy2api.test").warning("日志落盘自检")
        for handler in handlers:
            handler.flush()
        result = self.logs.read_service(lines=50)
        self.assertTrue(any("日志落盘自检" in e["message"] for e in result["entries"]))
        self.assertEqual(result["counts"].get("WARNING"), 1)
        entry = next(e for e in result["entries"] if "日志落盘自检" in e["message"])
        self.assertEqual(entry["logger"], "workbuddy2api.test")
        self.assertEqual(entry["level"], "WARNING")
        self.assertTrue(entry["time"].startswith("20"))
        for handler in handlers:
            logging.getLogger().removeHandler(handler)
            handler.close()


class ServiceLogParseTests(unittest.TestCase):
    def test_parses_time_level_logger_and_message(self):
        entries = parse_service_lines([
            "2026-10-08 18:06:30 INFO uvicorn.access 127.0.0.1 - \"POST /v1/chat/completions\" 200",
            "2026-10-08 18:06:31 ERROR uvicorn.error boom",
        ])
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0], {"time": "2026-10-08 18:06:30", "level": "INFO",
                                      "logger": "uvicorn.access",
                                      "message": "127.0.0.1 - \"POST /v1/chat/completions\" 200",
                                      "detail": ""})
        self.assertEqual(entries[1]["level"], "ERROR")

    def test_continuation_lines_attach_to_previous_entry(self):
        entries = parse_service_lines([
            "2026-10-08 18:06:31 ERROR uvicorn.error Traceback (most recent call last):",
            "  File \"x.py\", line 1, in <module>",
            "ValueError: boom",
            "2026-10-08 18:06:32 INFO uvicorn.access ok",
        ])
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["detail"].splitlines(),
                         ["  File \"x.py\", line 1, in <module>", "ValueError: boom"])
        self.assertEqual(entries[1]["detail"], "")

    def test_leading_orphan_lines_become_their_own_entry(self):
        entries = parse_service_lines(["orphan without timestamp", ""])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["message"], "orphan without timestamp")
        self.assertEqual(entries[0]["level"], "")

    def test_read_service_returns_newest_first_and_filters_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = LogStore(Path(tmp))
            logs.dir.mkdir(parents=True, exist_ok=True)
            (logs.dir / "service.log").write_text("\n".join([
                "2026-10-08 10:00:00 INFO uvicorn.access one",
                "2026-10-08 10:00:01 ERROR uvicorn.error two",
                "2026-10-08 10:00:02 INFO uvicorn.access three",
            ]) + "\n", encoding="utf-8")
            result = logs.read_service()
            self.assertEqual([e["message"].split()[-1] for e in result["entries"]],
                             ["three", "two", "one"])  # 最新在前
            self.assertEqual(result["counts"], {"INFO": 2, "ERROR": 1})
            errors = logs.read_service(level="ERROR")
            self.assertEqual([e["message"] for e in errors["entries"]], ["two"])
            self.assertEqual([e["logger"] for e in errors["entries"]], ["uvicorn.error"])
            # 计数基于全量，不受筛选影响
            self.assertEqual(errors["counts"], {"INFO": 2, "ERROR": 1})
            # 按来源筛选，loggers 统计按条数倒序
            by_logger = logs.read_service(logger="uvicorn.access")
            self.assertEqual([e["message"].split()[-1] for e in by_logger["entries"]], ["three", "one"])
            self.assertEqual(by_logger["loggers"],
                             [{"name": "uvicorn.access", "count": 2}, {"name": "uvicorn.error", "count": 1}])
            self.assertEqual(by_logger["counts"], {"INFO": 2, "ERROR": 1})
            # 级别与来源可叠加
            combined = logs.read_service(level="INFO", logger="uvicorn.error")
            self.assertEqual(combined["entries"], [])

    def test_read_service_without_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = LogStore(Path(tmp)).read_service()
            self.assertEqual(result["entries"], [])
            self.assertEqual(result["counts"], {})


class MetricsPersistenceTests(unittest.TestCase):
    def test_sink_receives_record_and_restore_fills_recent(self):
        written = []
        metrics = RequestMetrics(sink=written.append)
        metrics.begin()
        metrics.finish("/v1/chat/completions", "api", 200, True, 12.5, "success",
                       key="codex", model="hy3", credits=0)
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["model"], "hy3")
        self.assertEqual(written[0]["credits"], 0)

        restored = RequestMetrics()
        restored.restore(written)
        snapshot = restored.snapshot()
        self.assertEqual(snapshot["recent"][0]["model"], "hy3")
        # 累计计数器语义是「本次运行以来」，回填不应污染
        self.assertEqual(snapshot["completed"], 0)
        self.assertEqual(snapshot["in_flight"], 0)

    def test_restore_ignores_junk_and_respects_capacity(self):
        metrics = RequestMetrics()
        metrics.restore(["junk", None, {"model": "hy3"}] * 200)
        recent = metrics.snapshot()["recent"]
        self.assertEqual(len(recent), 100)
        self.assertTrue(all(r["model"] == "hy3" for r in recent))

    def test_finish_records_serving_account(self):
        written = []
        metrics = RequestMetrics(sink=written.append)
        metrics.begin()
        metrics.finish("/v1/chat/completions", "api", 200, True, 1.0, "success",
                       model="hy3", account="jywsdww@qq.com", site="intl")
        self.assertEqual(written[0]["account"], "jywsdww@qq.com")
        self.assertEqual(written[0]["site"], "intl")

    def test_sink_failure_never_breaks_the_request(self):
        def explode(_record):
            raise OSError("disk full")
        metrics = RequestMetrics(sink=explode)
        metrics.begin()
        metrics.finish("/v1/chat/completions", "api", 200, True, 1.0, "success")
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["completed"], 1)
        self.assertEqual(snapshot["in_flight"], 0)


if __name__ == "__main__":
    unittest.main()

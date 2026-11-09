import os
import tempfile
import time
import unittest

from core import ai, util
from core.config import Config
from core.index import Index
from core.recommend import CleanItem
from core.volumes import Volume


class SseTests(unittest.TestCase):
    def test_parse_delta(self):
        line = 'data: {"choices":[{"delta":{"content":"你好"}}]}'
        self.assertEqual(ai._parse_sse_line(line), ("content", "你好"))

    def test_parse_done(self):
        self.assertEqual(ai._parse_sse_line("data: [DONE]"), ai.DONE)

    def test_parse_blank(self):
        self.assertIsNone(ai._parse_sse_line(""))
        self.assertIsNone(ai._parse_sse_line("event: ping"))
        self.assertIsNone(ai._parse_sse_line(": keep-alive"))

    def test_parse_error_raises(self):
        with self.assertRaises(RuntimeError):
            ai._parse_sse_line('data: {"error":{"message":"bad key"}}')

    def test_parse_message_fallback(self):
        line = 'data: {"choices":[{"message":{"content":"hi"}}]}'
        self.assertEqual(ai._parse_sse_line(line), ("content", "hi"))

    def test_parse_reasoning(self):
        line = 'data: {"choices":[{"delta":{"content":null,"reasoning_content":"想一想"}}]}'
        self.assertEqual(ai._parse_sse_line(line), ("reasoning", "想一想"))


class ThinkSplitterTests(unittest.TestCase):
    def _run(self, chunks):
        sp = ai._ThinkSplitter()
        out = []
        for c in chunks:
            out.extend(sp.feed(c))
        out.extend(sp.flush())
        joined = {"content": "", "reasoning": ""}
        for kind, text in out:
            joined[kind] += text
        return joined

    def test_plain(self):
        self.assertEqual(self._run(["你好", "世界"]), {"content": "你好世界", "reasoning": ""})

    def test_think_split_across_chunks(self):
        got = self._run(["<thi", "nk>先分析", "一下</th", "ink>\n结论：可以删"])
        self.assertEqual(got["reasoning"], "先分析一下")
        self.assertEqual(got["content"], "结论：可以删")

    def test_think_tag_only_at_start(self):
        got = self._run(["答案里提到 <think> 不算"])
        self.assertEqual(got["content"], "答案里提到 <think> 不算")


class UrlTests(unittest.TestCase):
    def test_normalize(self):
        from core.config import normalize_base_url as n

        self.assertEqual(n("https://api.deepseek.com/v1/chat/completions"), "https://api.deepseek.com/v1")
        self.assertEqual(n("api.deepseek.com/"), "https://api.deepseek.com")
        self.assertEqual(n("127.0.0.1:11434/v1"), "http://127.0.0.1:11434/v1")
        self.assertEqual(n(""), "")

    def test_local(self):
        from core.config import is_local_url

        self.assertTrue(is_local_url("http://127.0.0.1:11434/v1"))
        self.assertTrue(is_local_url("http://localhost:1234/v1"))
        self.assertTrue(is_local_url("http://192.168.1.5:11434/v1"))
        self.assertFalse(is_local_url("https://api.deepseek.com/v1"))

    def test_candidates_add_v1(self):
        self.assertEqual(ai._base_candidates("https://api.deepseek.com"),
                         ["https://api.deepseek.com", "https://api.deepseek.com/v1"])
        self.assertEqual(ai._base_candidates("https://open.bigmodel.cn/api/paas/v4"),
                         ["https://open.bigmodel.cn/api/paas/v4"])


class FitContextTests(unittest.TestCase):
    def test_trims_lists_keeps_valid_json(self):
        import json

        ctx = {"磁盘": [{"盘符": "C:"}], "大文件Top": [{"路径": "C:\\x" * 50, "i": i} for i in range(200)]}
        text = ai._fit_context({"范围": "overview", "扫描上下文": ctx}, limit=4000)
        self.assertLessEqual(len(text), 4000)
        obj = json.loads(text)
        self.assertEqual(obj["扫描上下文"]["磁盘"][0]["盘符"], "C:")
        self.assertGreaterEqual(len(obj["扫描上下文"]["大文件Top"]), 3)


class OfflineReportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hdm_ai_")
        self.index = Index(os.path.join(self._tmp.name, "i.db"))
        now = int(time.time())
        from core.scanner import DirRec, FileRec

        self.index.replace_scan(
            [],
            [DirRec("C:\\Users", "C:\\", "C:", 2, 400 * 1024 ** 3, 1000, now)],
            [FileRec(r"C:\Users\wjx\data.pt", "data.pt", ".pt", "C:", r"C:\Users\wjx", 2 * 1024 ** 3, now, 0)],
            [FileRec(r"C:\Users\wjx\a.txt", "a.txt", ".txt", "C:", r"C:\Users\wjx", 10, now, 0)],
        )
        self.vol = Volume("C:", "", "NTFS", 449 * 1024 ** 3, 32 * 1024 ** 3)

    def tearDown(self):
        self.index.close()
        self._tmp.cleanup()

    def test_context(self):
        items = [CleanItem("临时文件", "用户临时目录", r"C:\Temp", 7 * 1024 ** 3, "safe", "临时", True, "dir_contents")]
        ctx = ai.build_context(self.index, [self.vol], items, mask=True)
        self.assertEqual(ctx["磁盘"][0]["盘符"], "C:")
        self.assertTrue(ctx["大文件Top"])
        self.assertEqual(ctx["清理候选"][0]["等级"], "安全")

    def test_offline_report(self):
        ctx = ai.build_context(self.index, [self.vol], [], mask=True)
        report = ai.offline_report(ctx)
        self.assertIn("磁盘现状", report)
        self.assertIn("C:", report)
        self.assertIn("建议", report)

    def test_offline_report_selection(self):
        ctx = ai.build_context(self.index, [self.vol], [], mask=True)
        report = ai.offline_report(ctx, [{"path": r"C:\Users\wjx\data.pt", "size": 2 * 1024 ** 3,
                                          "mtime": time.time(), "ext": ".pt"}])
        self.assertIn("选中项分析", report)
        self.assertIn("保留", report)

    def test_file_advice(self):
        iso = ai.file_advice(r"D:\win.iso", 1024 ** 3, time.time(), ".iso")
        self.assertIn("镜像", iso["类型"])
        pt = ai.file_advice(r"D:\model.pt", 1024 ** 3, time.time(), ".pt")
        self.assertIn("保留", pt["建议"])
        tmp = ai.file_advice(r"C:\Users\wjx\AppData\Local\Temp\a.tmp", 10, time.time(), ".tmp")
        self.assertEqual(tmp["建议"], "可删除")

    def test_chat_answer(self):
        ctx = ai.build_context(self.index, [self.vol], [], mask=True)
        answer = ai.offline_chat_answer("C 盘满了怎么办", ctx)
        self.assertIn("磁盘", answer)
        self.assertIn("C:\\Users", answer)

    def test_chat_clean_filters_drive(self):
        items = [CleanItem("临时文件", "用户临时目录", r"C:\Temp", 7 * 1024 ** 3, "safe", "临时", True, "dir_contents"),
                 CleanItem("缓存与开发", "D 盘缓存", r"D:\cache", 3 * 1024 ** 3, "safe", "缓存", True, "dir")]
        ctx = ai.build_context(self.index, [self.vol], items, mask=False)
        answer = ai.offline_chat_answer("C盘哪些可以删", ctx)
        self.assertIn("用户临时目录", answer)
        self.assertNotIn("D 盘缓存", answer)
        self.assertIn("7.00 GB", answer)

    def test_chat_known_item(self):
        items = [CleanItem("临时文件", "用户临时目录", r"C:\Temp", 7 * 1024 ** 3, "safe", "临时", True, "dir_contents")]
        ctx = ai.build_context(self.index, [self.vol], items, mask=False)
        answer = ai.offline_chat_answer("hiberfil.sys 能删吗", ctx)
        self.assertIn("powercfg -h off", answer)
        self.assertNotIn("用户临时目录", answer)

    def test_hotspot_drills_into_dominant_folder(self):
        from core.scanner import DirRec

        now = int(time.time())
        gb = 1024 ** 3
        self.index.replace_scan([], [
            DirRec("C:\\Users", "C:\\", "C:", 2, 300 * gb, 10, now),
            DirRec("C:\\Windows", "C:\\", "C:", 2, 40 * gb, 10, now),
            DirRec("C:\\Users\\wjx", "C:\\Users", "C:", 3, 290 * gb, 10, now),
            DirRec("C:\\Users\\wjx\\AppData", "C:\\Users\\wjx", "C:", 4, 90 * gb, 10, now),
            DirRec("C:\\Users\\wjx\\Videos", "C:\\Users\\wjx", "C:", 4, 80 * gb, 10, now),
        ], [], [])
        ctx = ai.build_context(self.index, [self.vol], [], mask=False)
        chain = ctx["大目录细分"]["C:"]
        self.assertEqual([spot["目录"] for spot in chain], ["C:\\Users\\wjx"])
        self.assertEqual([os.path.basename(f["目录"]) for f in chain[0]["子文件夹"]], ["AppData", "Videos"])
        answer = ai.offline_chat_answer("C盘为什么满了", ctx)
        self.assertIn("Videos", answer)
        self.assertIn("C:\\Users\\wjx\\Videos", ai.offline_chat_answer("能迁移什么到 D 盘", ctx))

    def test_chat_mentions_indexed_file(self):
        ctx = ai.build_context(self.index, [self.vol], [], mask=False)
        answer = ai.offline_chat_answer("data.pt 是什么", ctx)
        self.assertIn("模型权重", answer)

    def test_chat_recent_and_migrate(self):
        ctx = ai.build_context(self.index, [self.vol], [], mask=False)
        self.assertTrue(ctx["最近7天变动的大文件"])
        self.assertIn("data.pt", ai.offline_chat_answer("最近什么变大了", ctx))
        self.assertIn("目录联接", ai.offline_chat_answer("能迁移什么到 D 盘", ctx))

    def test_chat_help_and_empty_index(self):
        ctx = ai.build_context(self.index, [self.vol], [], mask=False)
        self.assertIn("C 盘为什么满了", ai.offline_chat_answer("你好", ctx))
        self.assertIn("完整扫描", ai.offline_chat_answer("C盘满了", {"磁盘": []}))

    def test_build_messages(self):
        ctx = {"磁盘": []}
        messages = ai.build_messages(ctx, "怎么清理", [], scope="overview")
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("磁盘空间顾问", messages[0]["content"])
        self.assertIn("怎么清理", messages[-1]["content"])

    def test_mask_in_context(self):
        ctx = ai.build_context(self.index, [self.vol], [], mask=True)
        joined = str(ctx)
        self.assertIn("<用户>", joined)


class ProviderTests(unittest.TestCase):
    def test_disabled_without_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(os.path.join(tmp, "c.json"))
            self.assertFalse(cfg.ai_ready())
            ok, msg, _base = ai.test_connection(ai.ApiSettings.from_config(cfg))
            self.assertFalse(ok)
            self.assertIn("API Key", msg)

    def test_local_ready_without_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(os.path.join(tmp, "c.json"))
            cfg.ai.update(base_url="http://127.0.0.1:11434/v1", model="qwen2.5:7b")
            self.assertTrue(cfg.ai_ready())


class _FakeServer:
    """本地假 OpenAI 接口：只在 /v1 下提供服务，用来验证补 /v1、流式、重试和模型列表。"""

    def __init__(self):
        import http.server
        import json
        import threading

        state = self.state = {"calls": 0, "fail_first": 0, "reject_temperature": False, "bodies": []}

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                pass

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/v1/models":
                    self._send(200, {"data": [{"id": "m-b"}, {"id": "m-a"}]})
                else:
                    self._send(404, {"error": {"message": "not found"}})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                state["calls"] += 1
                state["bodies"].append(body)
                if self.path != "/v1/chat/completions":
                    self._send(404, {"error": {"message": "not found"}})
                    return
                if state["fail_first"] > 0:
                    state["fail_first"] -= 1
                    self._send(503, {"error": {"message": "busy"}})
                    return
                if state["reject_temperature"] and "temperature" in body:
                    self._send(400, {"error": {"message": "Unsupported parameter: 'temperature'"}})
                    return
                if not body.get("stream"):
                    self._send(200, {"choices": [{"message": {"content": "OK"}}]})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                events = [
                    {"choices": [{"delta": {"reasoning_content": "想"}}]},
                    {"choices": [{"delta": {"content": "你"}}]},
                    {"choices": [{"delta": {"content": "好"}}]},
                ]
                lines = [f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events] + ["data: [DONE]\n\n"]
                for line in lines:
                    raw = line.encode()
                    self.wfile.write(f"{len(raw):x}\r\n".encode() + raw + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class OnlineClientTests(unittest.TestCase):
    def setUp(self):
        self.server = _FakeServer()
        self.settings = ai.ApiSettings(base_url=self.server.url, model="m-a", timeout=10)

    def tearDown(self):
        self.server.close()

    def test_stream_adds_v1_and_splits_reasoning(self):
        parts = list(ai.stream_chat(self.settings, [{"role": "user", "content": "hi"}]))
        self.assertEqual("".join(t for k, t in parts if k == "content"), "你好")
        self.assertEqual("".join(t for k, t in parts if k == "reasoning"), "想")

    def test_retry_on_503(self):
        self.server.state["fail_first"] = 1
        parts = list(ai.stream_chat(ai.ApiSettings(base_url=self.server.url + "/v1", model="m"), []))
        self.assertEqual("".join(t for k, t in parts if k == "content"), "你好")

    def test_drop_temperature_when_rejected(self):
        self.server.state["reject_temperature"] = True
        parts = list(ai.stream_chat(ai.ApiSettings(base_url=self.server.url + "/v1", model="m"), []))
        self.assertEqual("".join(t for k, t in parts if k == "content"), "你好")
        self.assertNotIn("temperature", self.server.state["bodies"][-1])

    def test_connection_reports_fixed_base(self):
        ok, msg, base = ai.test_connection(self.settings)
        self.assertTrue(ok, msg)
        self.assertEqual(base, self.server.url + "/v1")
        self.assertIn("OK", msg)

    def test_list_models(self):
        models, base = ai.list_models(self.settings)
        self.assertEqual(models, ["m-a", "m-b"])
        self.assertTrue(base.endswith("/v1"))

    def test_local_refused_message(self):
        port = self.server.httpd.server_address[1]
        self.server.close()
        bad = ai.ApiSettings(base_url=f"http://127.0.0.1:{port}/v1", model="m", timeout=3)
        with self.assertRaises(ai.ApiError) as ctx:
            list(ai.stream_chat(bad, []))
        self.assertIn("本地模型服务", str(ctx.exception))
        self.server = _FakeServer()


class ExtKindTests(unittest.TestCase):
    def test_ext_kind_covers_common(self):
        for ext in (".mp4", ".iso", ".zip", ".exe", ".dmp", ".pt", ".vmdk"):
            self.assertIn(ext, ai.EXT_KIND)


class MaskUserTests(unittest.TestCase):
    def test_mask_user_env(self):
        self.assertIn("<用户>", util.mask_user(r"C:\Users\someuser\x", "someuser"))

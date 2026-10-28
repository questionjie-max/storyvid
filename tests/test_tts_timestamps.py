"""任务 D2：TTS 时间戳通道（StepFun WS 词级 / MiniMax 句级 / 火山字级）+ 字幕注入。

全部离线：
- StepFun WS 用本地 mock WebSocket 服务器（stdlib socket 实现的 RFC6455
  服务端对端）验证握手、帧语义、事件状态机与时间戳解析；
- MiniMax / 火山用 monkeypatch 替换 requests.post / requests.get；
- 真实 Key 验证单独标注待办（TestRealKeyTodo，本机无 Key 自动跳过）。

通道契约（与 aligner.py 共用）：[{"word": str, "start": 段内相对秒, "end": 段内相对秒}]
"""
import base64
import binascii
import hashlib
import json
import socket
import struct
import threading

import os

import pytest

import config
import subtitles
import tts
import tts_ws
import video
from tts_ws import WebSocketError, WsClient

# conftest 的 autouse 围栏会把 tts.fetch_segment_timestamps 换成桩；
# 模块导入期捕获原函数，供「通道编排」用例装回（测试体晚于夹具执行，必赢）
_REAL_FETCH_SEGMENT_TIMESTAMPS = tts.fetch_segment_timestamps

_WORDS_CONTIGUOUS = [
    {"word": "今天天气真好。", "start": 0.0, "end": 1.2},
    {"word": "我们去公园玩吧！", "start": 1.2, "end": 3.0},
]


class _FakeJsonResponse:
    """模拟 requests.Response：本套件只用 json() 读响应体"""

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


# =====================================================================
# 本地 mock WebSocket 服务器（RFC6455 服务端对端，stdlib 实现）
# =====================================================================

class MockWsServer:
    """极简 WebSocket 服务端：握手 + 脚本化收发，供 WsClient / StepFun 通道测试。

    script 每项为元组：
      ("send", dict)   发送一帧 JSON 文本（服务端帧不掩码）
      ("raw", bytes)   发送一帧原始字节（构造分片/控制帧等非 JSON 场景）
      ("recv", n)      读取并记录 n 个客户端帧（客户端帧有掩码，自动解）
      ("ping",)        发一帧 Ping（验证客户端自动回 Pong）
      ("close",)       发 Close 帧后结束
    got_messages 记录客户端发来的 JSON 消息（断言 tts.create / tts.text.delta 用）；
    raw_frames 记录客户端帧原始 (opcode, payload)（断言 Pong 用）。
    """

    def __init__(self, script):
        self.script = list(script)
        self.got_messages: list = []
        self.raw_frames: list = []
        self._leftover = b""  # 握手读取多吞的客户端帧字节（回灌 _recv_exact）
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    # ---- 服务端帧原语 ----

    def _recv_exact(self, conn, n):
        buf = bytearray()
        # 先消费握手阶段多读的字节（客户端握手与首帧可能合并在同一段到达）
        if self._leftover:
            take = min(len(self._leftover), n)
            buf += self._leftover[:take]
            self._leftover = self._leftover[take:]
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("client closed")
            buf += chunk
        return bytes(buf)

    def _recv_frame(self, conn):
        b1, b2 = self._recv_exact(conn, 2)
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        length = b2 & 0x7F
        if length == 126:
            (length,) = struct.unpack(">H", self._recv_exact(conn, 2))
        elif length == 127:
            (length,) = struct.unpack(">Q", self._recv_exact(conn, 8))
        if masked:
            mask = self._recv_exact(conn, 4)
            payload = bytes(b ^ mask[i % 4]
                            for i, b in enumerate(self._recv_exact(conn, length)))
        else:
            payload = self._recv_exact(conn, length)
        return opcode, payload

    def _send_frame(self, conn, opcode, payload):
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([n])
        elif n < 65536:
            header += bytes([126]) + struct.pack(">H", n)
        else:
            header += bytes([127]) + struct.pack(">Q", n)
        conn.sendall(header + payload)

    def _run(self):
        conn, _ = self._sock.accept()
        try:
            # ---- RFC6455 握手 ----
            req = b""
            while b"\r\n\r\n" not in req:
                req += conn.recv(4096)
            # 握手与客户端首帧可能合并在同一段到达：多读的字节回灌帧读取器，
            # 与被测客户端 tts_ws.WsClient 的 _leftover 处理互为镜像
            head, _, self._leftover = req.partition(b"\r\n\r\n")
            req = head
            key = ""
            for line in req.decode("latin-1").split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            accept = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()).decode()
            conn.sendall((
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
            # ---- 执行脚本 ----
            for action in self.script:
                if action[0] == "send":
                    self._send_frame(conn, 0x1,
                                     json.dumps(action[1], ensure_ascii=False).encode())
                elif action[0] == "raw":
                    conn.sendall(action[1])
                elif action[0] == "recv":
                    for _ in range(action[1]):
                        opcode, payload = self._recv_frame(conn)
                        self.raw_frames.append((opcode, payload))
                        if opcode == 0x1:
                            text = payload.decode("utf-8", errors="replace")
                            try:
                                self.got_messages.append(json.loads(text))
                            except ValueError:
                                self.got_messages.append(text)  # 非 JSON 文本原样记录
                        elif opcode == 0x8:
                            return
                elif action[0] == "ping":
                    self._send_frame(conn, 0x9, b"hb")
                elif action[0] == "close":
                    self._send_frame(conn, 0x8, b"\x03\xe8")
                    return
        except (ConnectionError, OSError, ValueError) as e:
            # 对端时序类错误静默收场；其余异常打印出来（否则线程里无声失败，
            # 测试只会看到客户端侧的「连接被对端关闭」，无从排查）
            if not isinstance(e, (ConnectionError, BrokenPipeError)):
                import traceback
                print(f"[MockWsServer] 脚本异常: {e!r}")
                traceback.print_exc()
        finally:
            try:
                conn.close()
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass


class _RejectServer:
    """接受 TCP 连接但拒绝 WebSocket 升级（回 400），测握手失败路径"""

    def __init__(self):
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            conn, _ = self._sock.accept()
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            conn.close()
        except OSError:
            pass
        finally:
            try:
                self._sock.close()
            except OSError:
                pass


# =====================================================================
# tts_ws.WsClient：标准库 WebSocket 客户端（握手/帧/控制帧/分片/关闭）
# =====================================================================

class TestWsClient:
    def test_handshake_send_recv_ping_pong_close(self):
        """握手 → 收发文本 → Ping 自动回 Pong → 对端 Close 得到 None"""
        server = MockWsServer([
            ("send", {"type": "hello"}),
            ("recv", 1),           # 客户端第一条消息（顺便验证客户端帧掩码可解）
            ("ping",),
            ("send", {"type": "world"}),
            ("recv", 1),           # 客户端对 Ping 的 Pong 应答
            ("close",),
        ]).start()
        ws = WsClient(f"ws://127.0.0.1:{server.port}/ws", timeout=5.0)
        try:
            ws.send_text("你好")
            assert ws.recv_text() == '{"type": "hello"}'
            assert ws.recv_text() == '{"type": "world"}'
            assert ws.recv_text() is None  # 服务端 Close → None（不抛异常）
        finally:
            ws.close()
        # 客户端帧序：文本（你好）→ Pong（opcode 0xA，负载回显）
        opcodes = [op for op, _ in server.raw_frames]
        assert opcodes == [0x1, 0xA], f"帧序异常: {server.raw_frames}"
        assert server.raw_frames[1][1] == b"hb"

    def test_fragmented_message_assembled(self):
        """分片消息（FIN=0 文本 + CONT + FIN=1 CONT）聚合到 FIN 才返回完整文本"""
        server = MockWsServer([
            ("raw", b"\x01\x03abc"),      # FIN=0 opcode=1 "abc"
            ("raw", b"\x00\x03def"),      # FIN=0 opcode=0 "def"
            ("raw", b"\x80\x03ghi"),      # FIN=1 opcode=0 "ghi"
            ("close",),
        ]).start()
        ws = WsClient(f"ws://127.0.0.1:{server.port}/ws", timeout=5.0)
        try:
            assert ws.recv_text() == "abcdefghi"
            assert ws.recv_text() is None
        finally:
            ws.close()

    def test_unsolicited_pong_ignored(self):
        """服务端主动发 Pong：客户端忽略，不影响后续消息"""
        server = MockWsServer([
            ("raw", b"\x8a\x02ok"),       # Pong 帧（负载 "ok"）
            ("send", {"type": "after-pong"}),
            ("close",),
        ]).start()
        ws = WsClient(f"ws://127.0.0.1:{server.port}/ws", timeout=5.0)
        try:
            assert ws.recv_text() == '{"type": "after-pong"}'
        finally:
            ws.close()

    def test_handshake_rejected_raises(self):
        server = _RejectServer()
        with pytest.raises(WebSocketError):
            WsClient(f"ws://127.0.0.1:{server.port}/ws", timeout=5.0)

    def test_url_scheme_validated(self):
        with pytest.raises(WebSocketError):
            WsClient("https://example.com/ws", timeout=5.0)

    def test_connection_reset_raises(self):
        """对端握手后立即断开：recv_text 抛 WebSocketError 而不是挂死"""
        server = MockWsServer([]).start()  # 空脚本：握手后立即关闭连接
        ws = WsClient(f"ws://127.0.0.1:{server.port}/ws", timeout=5.0)
        try:
            with pytest.raises(WebSocketError):
                ws.recv_text()
        finally:
            ws.close()


# =====================================================================
# StepFun 词级时间戳通道（D2 ①）
# =====================================================================

def _stepfun_cfg(monkeypatch, server_port=None):
    monkeypatch.setattr(config, "STEPFUN_API_KEY", "test-key")
    monkeypatch.setattr(config, "TTS_MODEL", "step-tts-2")
    monkeypatch.setattr(config, "TTS_VOICE", "cixingnansheng")
    monkeypatch.setattr(config, "TTS_SAMPLE_RATE", 24000)
    monkeypatch.setattr(config, "TTS_SPEED", 1.15)
    monkeypatch.setattr(config, "TTS_VOLUME", 1.0)
    monkeypatch.setattr(config, "TTS_VOICE_LABEL", "")
    monkeypatch.setattr(config, "TTS_INSTRUCTION", "")
    if server_port is not None:
        monkeypatch.setattr(config, "STEPFUN_WS_URL",
                            f"ws://127.0.0.1:{server_port}/v1/realtime/audio")


class TestStepFunWordTimestamps:
    def test_full_flow_against_mock_server(self, monkeypatch):
        """连接 → tts.create(timestamp=true) → 收 subtitle → audio.done 结束"""
        server = MockWsServer([
            ("send", {"type": "tts.connection.done",
                      "data": {"session_id": "sess-1"}}),
            ("recv", 1),  # tts.create
            ("send", {"type": "tts.response.created",
                      "data": {"session_id": "sess-1"}}),
            ("recv", 2),  # tts.text.delta + tts.text.done
            ("send", {"type": "tts.response.audio.delta",
                      "data": {"audio": "QUJD"}}),  # 音频分片：通道只取时间戳，应忽略
            ("send", {"type": "tts.response.subtitle", "data": {"items": [
                {"text": "今天", "start_time": 0, "end_time": 320},
                {"text": "天气", "start_time": 320, "end_time": 640}]}}),
            ("send", {"type": "tts.response.subtitle", "data": {"items": [
                {"text": "真好", "start_time": 640, "end_time": 1000}]}}),
            ("send", {"type": "tts.response.audio.done", "data": {"audio": "QUJD"}}),
            ("close",),
        ]).start()
        _stepfun_cfg(monkeypatch, server.port)

        words = tts.fetch_stepfun_word_timestamps("今天天气真好", voice="")

        assert words == [
            {"word": "今天", "start": 0.0, "end": 0.32},
            {"word": "天气", "start": 0.32, "end": 0.64},
            {"word": "真好", "start": 0.64, "end": 1.0},
        ]
        # 客户端发出的指令序列与结构（按官方文档字段）
        assert [m["type"] for m in server.got_messages] == [
            "tts.create", "tts.text.delta", "tts.text.done"]
        create = server.got_messages[0]["data"]
        assert create["session_id"] == "sess-1"
        assert create["timestamp"] is True
        assert create["mode"] == "sentence"
        assert create["voice_id"] == "cixingnansheng"
        assert create["sample_rate"] == 24000
        assert server.got_messages[1]["data"]["session_id"] == "sess-1"
        assert server.got_messages[1]["data"]["text"] == "今天天气真好"

    def test_ws_url_model_query_param(self, monkeypatch):
        """model 经 URL 查询参数传入（官方约定）"""
        _stepfun_cfg(monkeypatch)
        assert tts._stepfun_ws_url() == (
            "wss://api.stepfun.com/v1/realtime/audio?model=step-tts-2")
        monkeypatch.setattr(config, "STEPFUN_WS_URL", "ws://127.0.0.1:1/rt?foo=1")
        assert tts._stepfun_ws_url().endswith("/rt?foo=1&model=step-tts-2")

    def test_missing_key_skips_gracefully(self, monkeypatch, capsys):
        """Key 缺失优雅跳过（任务要求），不出网、返回 None"""
        _stepfun_cfg(monkeypatch)
        monkeypatch.setattr(config, "STEPFUN_API_KEY", "")
        assert tts.fetch_stepfun_word_timestamps("文本") is None
        assert "未配置 STEPFUN_API_KEY" in capsys.readouterr().out

    def test_unreachable_host_returns_none(self, monkeypatch):
        """连接失败（不存在的端口）→ 优雅返回 None，绝不抛异常"""
        _stepfun_cfg(monkeypatch)
        monkeypatch.setattr(config, "STEPFUN_WS_URL", "ws://127.0.0.1:1/rt")
        assert tts.fetch_stepfun_word_timestamps("文本") is None

    def test_error_event_returns_none(self, monkeypatch, capsys):
        """tts.response.error → 优雅失败返回 None（绝不抛异常）"""
        server = MockWsServer([
            ("send", {"type": "tts.connection.done",
                      "data": {"session_id": "sess-1"}}),
            ("recv", 1),
            ("send", {"type": "tts.response.created", "data": {}}),
            ("recv", 2),
            ("send", {"type": "tts.response.error",
                      "data": {"code": "503", "message": "engine overloaded"}}),
            ("close",),
        ]).start()
        _stepfun_cfg(monkeypatch, server.port)
        assert tts.fetch_stepfun_word_timestamps("文本") is None
        assert "StepFun WS 错误 503" in capsys.readouterr().out

    def test_connection_closed_before_done_returns_none(self, monkeypatch):
        """连接提前关闭（没等到 audio.done）→ 视为失败，不交出残缺时间戳"""
        server = MockWsServer([
            ("send", {"type": "tts.connection.done",
                      "data": {"session_id": "sess-1"}}),
            ("recv", 1),
            ("close",),
        ]).start()
        _stepfun_cfg(monkeypatch, server.port)
        assert tts.fetch_stepfun_word_timestamps("文本") is None

    def test_long_text_truncated_like_http_path(self, monkeypatch):
        """>1000 字符按与 HTTP 音频路径同规则截断（保证两路文本一致）"""
        server = MockWsServer([
            ("send", {"type": "tts.connection.done", "data": {"session_id": "s"}}),
            ("recv", 1),
            ("send", {"type": "tts.response.created", "data": {}}),
            ("recv", 2),
            ("send", {"type": "tts.response.audio.done", "data": {}}),
            ("close",),
        ]).start()
        _stepfun_cfg(monkeypatch, server.port)
        words = tts.fetch_stepfun_word_timestamps("长" * 1100)
        assert words is None  # 无 subtitle 事件 → None
        sent = server.got_messages[1]["data"]["text"]
        assert len(sent) <= 1000
        assert sent.endswith("...")

    def test_malformed_subtitle_items_skipped(self):
        """items 里缺字段/非法时间的记录跳过，不炸解析"""
        event = {"type": "tts.response.subtitle", "data": {"items": [
            {"text": "好", "start_time": 0, "end_time": 100},
            {"text": "", "start_time": 100, "end_time": 200},    # 空文本跳过
            {"text": "坏", "start_time": "x", "end_time": 200},  # 非数值跳过
            {"text": "倒", "start_time": 300, "end_time": 200},  # end<start 跳过
            "not-a-dict",
            {"text": "尾", "start_time": 200, "end_time": 300},
        ]}}
        assert tts._stepfun_subtitle_items(event) == [
            {"word": "好", "start": 0.0, "end": 0.1},
            {"word": "尾", "start": 0.2, "end": 0.3},
        ]


# =====================================================================
# MiniMax 句级时间戳通道（D2 ②）
# =====================================================================

def _minimax_cfg(monkeypatch):
    monkeypatch.setattr(config, "MINIMAX_API_KEY", "mm-key")
    monkeypatch.setattr(config, "MINIMAX_BASE_URL", "https://api.minimaxi.com")
    monkeypatch.setattr(config, "MINIMAX_TTS_MODEL", "speech-01-hd")
    monkeypatch.setattr(config, "MINIMAX_TTS_VOICE_ID", "male-qn-jingying")
    monkeypatch.setattr(config, "MINIMAX_TTS_SPEED", 1.15)


class TestMinimaxSentenceTimestamps:
    def test_full_flow_with_subtitle_file(self, monkeypatch):
        """带字幕参数请求 → 下载 subtitle_file → 句级时间片（毫秒转秒）"""
        _minimax_cfg(monkeypatch)
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"], captured["json"] = url, json
            return _FakeJsonResponse({
                "base_resp": {"status_code": 0, "status_msg": "success"},
                "data": {"audio": binascii.hexlify(b"x").decode(),
                         "subtitle_file": "https://cdn.example/sub.json"},
            })

        def fake_get(url, timeout=None):
            captured["subtitle_url"] = url
            return _FakeJsonResponse({"subtitles": [
                {"text": "今天天气真好。", "time_begin": 0, "time_end": 1200},
                {"text": "我们去公园玩吧！", "time_begin": 1200, "time_end": 3000},
            ]})

        monkeypatch.setattr(tts.requests, "post", fake_post)
        monkeypatch.setattr(tts.requests, "get", fake_get)

        words = tts.fetch_minimax_sentence_timestamps("今天天气真好。我们去公园玩吧！",
                                                      voice="v1")
        assert captured["json"]["subtitle_enable"] is True
        assert captured["json"]["subtitle_type"] == "sentence"
        assert captured["json"]["stream"] is False
        assert captured["json"]["voice_setting"]["voice_id"] == "v1"
        assert captured["subtitle_url"] == "https://cdn.example/sub.json"
        assert words == [
            {"word": "今天天气真好。", "start": 0.0, "end": 1.2},
            {"word": "我们去公园玩吧！", "start": 1.2, "end": 3.0},
        ]

    def test_audio_request_payload_has_no_subtitle_params(self, monkeypatch, tmp_path):
        """回归护栏：正式音频请求绝不携带字幕参数（未实测参数不上主路径）"""
        _minimax_cfg(monkeypatch)
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["json"] = json
            return _FakeJsonResponse({
                "base_resp": {"status_code": 0},
                "data": {"audio": binascii.hexlify(b"x").decode()},
            })

        monkeypatch.setattr(tts.requests, "post", fake_post)
        out = tmp_path / "a.mp3"
        tts.generate_speech_minimax("文本", str(out), voice="v")
        assert "subtitle_enable" not in captured["json"]
        assert "subtitle_type" not in captured["json"]
        assert out.read_bytes() == b"x"

    def test_missing_subtitle_file_returns_none(self, monkeypatch, capsys):
        _minimax_cfg(monkeypatch)
        monkeypatch.setattr(tts.requests, "post",
                            lambda *a, **k: _FakeJsonResponse(
                                {"base_resp": {"status_code": 0},
                                 "data": {"audio": "78"}}))
        assert tts.fetch_minimax_sentence_timestamps("文本") is None
        assert "subtitle_file" in capsys.readouterr().out

    def test_api_error_returns_none(self, monkeypatch, capsys):
        _minimax_cfg(monkeypatch)
        monkeypatch.setattr(tts.requests, "post",
                            lambda *a, **k: _FakeJsonResponse(
                                {"base_resp": {"status_code": 1004,
                                               "status_msg": "auth failed"}}))
        assert tts.fetch_minimax_sentence_timestamps("文本") is None
        assert "auth failed" in capsys.readouterr().out

    def test_download_failure_returns_none(self, monkeypatch):
        _minimax_cfg(monkeypatch)
        monkeypatch.setattr(tts.requests, "post",
                            lambda *a, **k: _FakeJsonResponse(
                                {"base_resp": {"status_code": 0},
                                 "data": {"audio": "78",
                                          "subtitle_file": "https://cdn/x.json"}}))

        def boom(*a, **k):
            raise OSError("network down")

        monkeypatch.setattr(tts.requests, "get", boom)
        assert tts.fetch_minimax_sentence_timestamps("文本") is None

    def test_missing_key_skips_gracefully(self, monkeypatch, capsys):
        _minimax_cfg(monkeypatch)
        monkeypatch.setattr(config, "MINIMAX_API_KEY", "")
        assert tts.fetch_minimax_sentence_timestamps("文本") is None
        assert "未配置 MINIMAX_API_KEY" in capsys.readouterr().out


class TestParseTimestampFileJson:
    """宽松字幕解析（MiniMax subtitle_file / 火山 addition 共用，结构未实测）"""

    def test_top_level_list(self):
        data = [{"text": "一句", "time_begin": 0, "time_end": 1500},
                {"text": "两句", "time_begin": 1500, "time_end": 2600}]
        assert tts.parse_timestamp_file_json(data) == [
            {"word": "一句", "start": 0.0, "end": 1.5},
            {"word": "两句", "start": 1.5, "end": 2.6},
        ]

    def test_nested_dict_various_keys(self):
        assert tts.parse_timestamp_file_json({"data": [
            {"word": "词", "begin_time": "120", "end_time": "800"}]}) == [
            {"word": "词", "start": 0.12, "end": 0.8}]
        assert tts.parse_timestamp_file_json({"addition": {"rows": [
            {"char": "字", "start_time": 0, "end_time": 99}]}}) == [
            {"word": "字", "start": 0.0, "end": 0.099}]

    def test_time_field_pair_fallback(self):
        # time_begin/time_end 缺失时回退 begin_time/end_time
        assert tts.parse_timestamp_file_json([
            {"text": "x", "begin_time": 0, "end_time": 500}]) == [
            {"word": "x", "start": 0.0, "end": 0.5}]

    def test_invalid_elements_skipped_and_empty_returns_none(self):
        assert tts.parse_timestamp_file_json([
            {"text": "", "time_begin": 0, "time_end": 100},      # 空文本
            {"text": "无时间"},
            {"text": "倒", "time_begin": 200, "time_end": 100},  # end < begin
            {"text": "半", "time_begin": 0, "time_end": None},   # 半缺
            "junk",
        ]) is None
        assert tts.parse_timestamp_file_json({"no": "lists here"}) is None
        assert tts.parse_timestamp_file_json(None) is None


# =====================================================================
# 火山引擎字级时间戳通道（D2 ③，best-effort，真实 Key 验证待办）
# =====================================================================

def _volc_cfg(monkeypatch):
    monkeypatch.setattr(config, "VOLC_TTS_APPID", "test-appid")
    monkeypatch.setattr(config, "VOLC_TTS_TOKEN", "test-token")
    monkeypatch.setattr(config, "VOLC_TTS_API_URL",
                        "https://openspeech.bytedance.com/api/v1/tts")
    monkeypatch.setattr(config, "VOLC_TTS_CLUSTER", "volcano_tts")
    monkeypatch.setattr(config, "VOLC_TTS_VOICE_TYPE", "BV001_streaming")
    monkeypatch.setattr(config, "VOLC_TTS_ENCODING", "mp3")
    monkeypatch.setattr(config, "VOLC_TTS_SPEED_RATIO", 1.0)
    monkeypatch.setattr(config, "VOLC_TTS_VOLUME_RATIO", 1.0)


class TestVolcWordTimestamps:
    def test_request_carries_with_timestamp_and_parses_addition(self, monkeypatch):
        _volc_cfg(monkeypatch)
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["json"] = json
            return _FakeJsonResponse({
                "code": 3000, "message": "success",
                "data": base64.b64encode(b"x").decode(),
                # addition 结构未实测：按宽松解析器支持的形状构造 mock
                "addition": {"with_timestamp": [
                    {"text": "你", "start_time": 0, "end_time": 180},
                    {"text": "好", "start_time": 180, "end_time": 420},
                ]},
            })

        monkeypatch.setattr(tts.requests, "post", fake_post)
        words = tts.fetch_volc_word_timestamps("你好", voice="BV001_streaming")
        assert captured["json"]["request"]["with_timestamp"] == 1
        assert words == [{"word": "你", "start": 0.0, "end": 0.18},
                         {"word": "好", "start": 0.18, "end": 0.42}]

    def test_audio_request_has_no_with_timestamp(self, monkeypatch, tmp_path):
        """回归护栏：正式音频请求不带 with_timestamp"""
        _volc_cfg(monkeypatch)
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["json"] = json
            return _FakeJsonResponse({"code": 3000, "message": "success",
                                      "data": base64.b64encode(b"x").decode()})

        monkeypatch.setattr(tts.requests, "post", fake_post)
        tts.generate_speech_volc("文本", str(tmp_path / "v.mp3"))
        assert "with_timestamp" not in captured["json"]["request"]

    def test_error_or_missing_addition_returns_none(self, monkeypatch, capsys):
        _volc_cfg(monkeypatch)
        monkeypatch.setattr(tts.requests, "post",
                            lambda *a, **k: _FakeJsonResponse(
                                {"code": 3001, "message": "bad"}))
        assert tts.fetch_volc_word_timestamps("文本") is None
        monkeypatch.setattr(tts.requests, "post",
                            lambda *a, **k: _FakeJsonResponse(
                                {"code": 3000, "message": "success", "data": "eA=="}))
        assert tts.fetch_volc_word_timestamps("文本") is None
        assert "未解析出时间戳" in capsys.readouterr().out

    def test_missing_credentials_skips(self, monkeypatch, capsys):
        _volc_cfg(monkeypatch)
        monkeypatch.setattr(config, "VOLC_TTS_APPID", "")
        assert tts.fetch_volc_word_timestamps("文本") is None
        assert "未配置 VOLC_TTS_APPID" in capsys.readouterr().out


# =====================================================================
# 通道编排与 generate_all_speech 接线
# =====================================================================

class TestFetchSegmentTimestamps:
    def test_dispatch_per_provider(self, monkeypatch):
        """按 provider 分发到对应通道；契约时间片原样透传"""
        monkeypatch.setattr(tts, "fetch_segment_timestamps",
                            _REAL_FETCH_SEGMENT_TIMESTAMPS)
        fake = [{"word": "词", "start": 0.0, "end": 1.0}]
        calls = []
        monkeypatch.setattr(tts, "fetch_stepfun_word_timestamps",
                            lambda text, voice="": calls.append("stepfun") or fake)
        monkeypatch.setattr(tts, "fetch_minimax_sentence_timestamps",
                            lambda text, voice="": calls.append("minimax") or None)
        monkeypatch.setattr(tts, "fetch_volc_word_timestamps",
                            lambda text, voice="": calls.append("volc") or None)

        monkeypatch.setattr(config, "TTS_PROVIDER", "stepfun")
        assert tts.fetch_segment_timestamps("文本", voice="v") == fake
        monkeypatch.setattr(config, "TTS_PROVIDER", "minimax")
        assert tts.fetch_segment_timestamps("文本", voice="v") is None
        monkeypatch.setattr(config, "TTS_PROVIDER", "volc")
        assert tts.fetch_segment_timestamps("文本", voice="v") is None
        assert calls == ["stepfun", "minimax", "volc"]

    def test_unknown_provider_returns_none(self, monkeypatch):
        monkeypatch.setattr(tts, "fetch_segment_timestamps",
                            _REAL_FETCH_SEGMENT_TIMESTAMPS)
        monkeypatch.setattr(config, "TTS_PROVIDER", "nope")
        assert tts.fetch_segment_timestamps("文本") is None

    def test_master_switch_off(self, monkeypatch):
        """TTS_TIMESTAMPS=0 全局关闭：不出网直接 None"""
        monkeypatch.setattr(tts, "fetch_segment_timestamps",
                            _REAL_FETCH_SEGMENT_TIMESTAMPS)
        monkeypatch.setattr(config, "TTS_PROVIDER", "stepfun")
        monkeypatch.setattr(config, "TTS_TIMESTAMPS", False)
        monkeypatch.setattr(tts, "fetch_stepfun_word_timestamps",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("关闭后不应调用通道")))
        assert tts.fetch_segment_timestamps("文本") is None

    def test_explicit_provider_overrides_config(self, monkeypatch):
        monkeypatch.setattr(tts, "fetch_segment_timestamps",
                            _REAL_FETCH_SEGMENT_TIMESTAMPS)
        monkeypatch.setattr(config, "TTS_PROVIDER", "stepfun")
        monkeypatch.setattr(tts, "fetch_minimax_sentence_timestamps",
                            lambda text, voice="": [{"word": "句", "start": 0.0, "end": 1.0}])
        monkeypatch.setattr(tts, "fetch_stepfun_word_timestamps",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("显式 provider 应优先于 config")))
        assert tts.fetch_segment_timestamps("文本", provider="minimax") == [
            {"word": "句", "start": 0.0, "end": 1.0}]


class TestGenerateAllSpeechChannel:
    """generate_all_speech 写 seg["words"]（words_source="tts"），失败不影响配音"""

    def _pipeline_mocks(self, monkeypatch, tmp_path, fetch):
        monkeypatch.setattr(config, "PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(config, "TEMP_DIR", str(tmp_path))
        monkeypatch.setattr(tts, "get_audio_duration", lambda p: 3.0)

        def fake_generate_speech(text, output_path, provider="", voice=""):
            with open(output_path, "wb"):
                pass

        monkeypatch.setattr(tts, "generate_speech", fake_generate_speech)
        monkeypatch.setattr(tts, "normalize_loudness", lambda p: p)
        monkeypatch.setattr(tts, "fetch_segment_timestamps", fetch)

    def test_words_channel_written(self, monkeypatch, tmp_path):
        words = [{"word": "词", "start": 0.0, "end": 1.0}]
        seen = []

        def fetch(text, provider="", voice="", audio_path=""):
            seen.append((text, provider, voice))
            return words if "有词" in text else None

        self._pipeline_mocks(monkeypatch, tmp_path, fetch)
        segments = [
            {"text": "第一段有词时间戳。", "duration": 0, "image_keyword": ""},
            {"text": "第二段没有时间戳啊。", "duration": 0, "image_keyword": ""},
        ]
        result = tts.generate_all_speech(segments, provider="minimax")
        assert result[0]["words"] == words
        assert result[0]["words_source"] == "tts"
        assert "words" not in result[1]
        assert seen and all(p == "minimax" for _, p, _ in seen)

    def test_channel_crash_does_not_break_audio(self, monkeypatch, tmp_path):
        def boom(*a, **k):
            raise RuntimeError("通道崩溃")

        self._pipeline_mocks(monkeypatch, tmp_path, boom)
        segments = [{"text": "通道会崩溃的段落。", "duration": 0, "image_keyword": ""}]
        result = tts.generate_all_speech(segments)
        assert result[0]["audio_path"]          # 配音照常完成
        assert not result[0]["audio_error"]
        assert "words" not in result[0]

    def test_reused_segment_gets_no_words(self, monkeypatch, tmp_path):
        calls = []

        def fetch(text, provider="", voice="", audio_path=""):
            calls.append(text)
            return [{"word": "词", "start": 0.0, "end": 1.0}]

        self._pipeline_mocks(monkeypatch, tmp_path, fetch)
        pre = tmp_path / "segment_000.mp3"
        pre.write_bytes(b"pre-existing")
        # 文本快照边车：与本次段落文本一致才可复用（见 tts._audio_matches_text）
        pre.with_suffix(".mp3.sha1").write_text(
            hashlib.sha1("复用的旧音频段。".encode("utf-8")).hexdigest(), encoding="utf-8")
        segments = [{"text": "复用的旧音频段。", "duration": 0, "image_keyword": ""}]
        result = tts.generate_all_speech(segments)
        assert result[0]["audio_reused"] is True
        assert "words" not in result[0]   # 旧音频与本次请求无对应关系，不伪造
        assert calls == []


# =====================================================================
# 字幕注入：build_chunk_durations(words=...) / pad / generate_srt / 全链路
# =====================================================================

class TestBuildChunkDurationsWithWords:
    def test_word_level_real_boundaries(self):
        """词级时间片：块边界落在词/句边界上 → 真实时长；句间停顿归前块窗口"""
        text = "今天天气真好。我们去公园玩吧！"
        words = [
            {"word": "今天", "start": 0.0, "end": 0.4},
            {"word": "天气", "start": 0.4, "end": 0.8},
            {"word": "真好", "start": 0.8, "end": 1.2},
            {"word": "我们", "start": 1.5, "end": 1.9},
            {"word": "去公园", "start": 1.9, "end": 2.6},
            {"word": "玩吧", "start": 2.6, "end": 3.0},
        ]
        result = subtitles.build_chunk_durations(text, 3.5, words=words)
        assert [c for c, _ in result] == ["今天天气真好。", "我们去公园玩吧！"]
        # 第二句从 1.5s 起说 → 第一块窗口 [0, 1.5)，第二块 [1.5, 3.0)，
        # 尾部 0.5s 静音不上字幕（和 < 3.5）
        assert result[0][1] == pytest.approx(1.5)
        assert result[1][1] == pytest.approx(1.5)
        assert sum(d for _, d in result) == pytest.approx(3.0)

    def test_sentence_level_contiguous(self):
        """句级时间片（MiniMax 口径，连续无间隙）同样按真实边界切"""
        result = subtitles.build_chunk_durations("今天天气真好。我们去公园玩吧！",
                                                 3.5, words=_WORDS_CONTIGUOUS)
        assert result[0][1] == pytest.approx(1.2)
        assert result[1][1] == pytest.approx(1.8)

    def test_text_key_fallback(self):
        """兼容 {"text": ...} 键名（宽容通道变体）"""
        words = [{"text": "今天天气真好", "start": 0.0, "end": 1.2},
                 {"text": "我们去公园玩吧", "start": 1.2, "end": 3.0}]
        result = subtitles.build_chunk_durations("今天天气真好。我们去公园玩吧！",
                                                 3.5, words=words)
        assert result[0][1] == pytest.approx(1.2)

    def test_no_words_falls_back_to_proportional(self):
        """缺省 words：字符比例旧行为不变（总和 = 段时长）"""
        result = subtitles.build_chunk_durations("今天天气真好。我们去公园玩吧！", 3.5)
        assert sum(d for _, d in result) == pytest.approx(3.5)
        # 比例路径与真实边界结果不同（证明 words 确实生效的对照）
        assert result[0][1] != pytest.approx(1.2)

    def test_fallback_on_text_mismatch(self):
        """时间片文本与段落文本归一长度差超 25% → 回退字符比例"""
        words = [{"word": "啊啊", "start": 0.0, "end": 3.0}]
        result = subtitles.build_chunk_durations("今天天气真好。我们去公园玩吧！",
                                                 3.5, words=words)
        assert sum(d for _, d in result) == pytest.approx(3.5)

    def test_fallback_when_times_exceed_duration(self):
        """时间戳超出段时长 5%（疑似单位错/不同源）→ 回退"""
        words = [{"word": "今天天气真好", "start": 0.0, "end": 1.2},
                 {"word": "我们去公园玩吧", "start": 1.2, "end": 7.0}]
        result = subtitles.build_chunk_durations("今天天气真好。我们去公园玩吧！",
                                                 3.5, words=words)
        assert sum(d for _, d in result) == pytest.approx(3.5)

    def test_fallback_when_coverage_too_low(self):
        """时间片只覆盖开头（长度匹配但提前结束）→ 回退，防止字幕挤在开头"""
        words = [{"word": "今天天气真好我们去公园玩吧", "start": 0.0, "end": 1.0}]
        result = subtitles.build_chunk_durations("今天天气真好。我们去公园玩吧！",
                                                 3.5, words=words)
        assert sum(d for _, d in result) == pytest.approx(3.5)

    @pytest.mark.parametrize("words", [
        [],
        None,
        ["not-a-dict"],
        {"word": "x", "start": 0.0, "end": 1.0},            # 不是列表
        [{"word": "x", "start": "abc", "end": 1.0}],        # 时间不可解析
        [{"word": "x", "start": 2.0, "end": 1.0}],          # end < start
        [{"word": "x", "start": -1.0, "end": 1.0}],         # 负起点
        [{"start": 0.0, "end": 1.0}],                       # 缺文本
        [{"word": "", "start": 0.0, "end": 1.0}],           # 空文本
    ])
    def test_bad_words_shapes_fall_back(self, words):
        result = subtitles.build_chunk_durations("今天天气真好。我们去公园玩吧！",
                                                 3.5, words=words)
        assert [c for c, _ in result] == ["今天天气真好。", "我们去公园玩吧！"]
        assert sum(d for _, d in result) == pytest.approx(3.5)

    def test_single_chunk_ignores_words(self):
        assert subtitles.build_chunk_durations(
            "一句话。", 7.5, words=_WORDS_CONTIGUOUS) == [("一句话。", 7.5)]

    def test_durations_positive_and_bounded(self):
        """整段时间片内的两块：0.05s 下限保护，无零/负块；总和不超过段时长太多"""
        words = [{"word": "今天天气真好我们去公园玩吧", "start": 0.5, "end": 3.0}]
        result = subtitles.build_chunk_durations("今天天气真好。我们去公园玩吧！",
                                                 3.0, words=words)
        assert all(d >= 0.05 for _, d in result)
        assert sum(d for _, d in result) <= 3.0 + 0.2  # 仅 0.05s 下限累计的余量


class TestPadChunkDurations:
    def test_extends_last_chunk_when_short(self):
        chunks = [("a", 1.0), ("b", 2.0)]
        assert subtitles.pad_chunk_durations(chunks, 4.0) == [("a", 1.0), ("b", 3.0)]

    def test_scales_down_when_over(self):
        chunks = [("a", 1.0), ("b", 2.0)]
        padded = subtitles.pad_chunk_durations(chunks, 2.0)
        assert padded[0][1] == pytest.approx(2.0 / 3)
        assert padded[1][1] == pytest.approx(4.0 / 3)
        assert sum(d for _, d in padded) == pytest.approx(2.0)

    def test_noop_when_equal(self):
        chunks = [("a", 1.0), ("b", 2.0)]
        assert subtitles.pad_chunk_durations(chunks, 3.0) == chunks

    def test_degenerate_inputs(self):
        assert subtitles.pad_chunk_durations([], 3.0) == []
        assert subtitles.pad_chunk_durations([("a", 0.0)], 0.0) == [("a", 0.0)]


class TestGenerateSrtWithWords:
    def test_srt_uses_real_boundaries(self, tmp_path):
        """SRT 各条字幕按真实发音边界落时间（而非字符比例）"""
        seg = {"text": "今天天气真好。我们去公园玩吧！", "duration": 3.5,
               "words": _WORDS_CONTIGUOUS}
        out = tmp_path / "subtitles.srt"
        subtitles.generate_srt([seg], str(out))
        lines = out.read_text(encoding="utf-8").splitlines()
        # SRT 块结构：序号 / 时间轴 / 文本 / 空行（每块 4 行）
        assert lines[0] == "1" and lines[3] == ""
        assert lines[1] == "00:00:00,000 --> 00:00:01,200"
        assert lines[2] == "今天天气真好。"
        assert lines[5] == "00:00:01,200 --> 00:00:03,000"
        assert lines[6] == "我们去公园玩吧！"

    def test_srt_without_words_unchanged(self, tmp_path):
        """没有 words 的段：旧行为（字符比例、总和=段时长）不受影响"""
        seg = {"text": "今天天气真好。我们去公园玩吧！", "duration": 3.5}
        out = tmp_path / "subtitles.srt"
        subtitles.generate_srt([seg], str(out))
        lines = out.read_text(encoding="utf-8").splitlines()
        assert lines[5] != "00:00:01,200 --> 00:00:03,000"
        assert lines[5].endswith("00:00:03,500")


class TestFullChainCliAndWeb:
    def test_generate_all_speech_then_generate_srt(self, monkeypatch, tmp_path):
        """CLI/Web 全链路（main.py 与 web/app.py 都是 generate_all_speech 后跟
        generate_srt、段对象原样流转）：TTS 通道产出 seg["words"] 后，SRT 自动
        按真实边界落时间，调用方零改动"""
        monkeypatch.setattr(config, "PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(config, "TEMP_DIR", str(tmp_path))
        monkeypatch.setattr(tts, "get_audio_duration", lambda p: 3.5)

        def fake_generate_speech(text, output_path, provider="", voice=""):
            with open(output_path, "wb"):
                pass

        monkeypatch.setattr(tts, "generate_speech", fake_generate_speech)
        monkeypatch.setattr(tts, "normalize_loudness", lambda p: p)
        monkeypatch.setattr(tts, "fetch_segment_timestamps",
                            lambda text, provider="", voice="", audio_path="": _WORDS_CONTIGUOUS)

        segments = [{"text": "今天天气真好。我们去公园玩吧！",
                     "duration": 0, "image_keyword": ""}]
        segments = tts.generate_all_speech(segments, provider="minimax",
                                           project_dir=str(tmp_path))
        assert segments[0]["words_source"] == "tts"

        srt_path = tmp_path / "subtitles.srt"
        subtitles.generate_srt(segments, str(srt_path))  # 与 main.py / web 同参调用
        lines = srt_path.read_text(encoding="utf-8").splitlines()
        assert lines[1] == "00:00:00,000 --> 00:00:01,200"
        assert lines[5] == "00:00:01,200 --> 00:00:03,000"

    def test_video_segment_clip_uses_real_boundaries(self, monkeypatch, tmp_path):
        """video 侧消费点（video._build_segment_clip → build_chunk_durations
        (words=...) + pad_chunk_durations）：seg["words"] 传入后叠加字幕按
        真实发音边界上屏、尾块 pad 兜住整段。

        此前该路径只有 generate_srt 侧有链路测试（B 队指出的缺口）；
        KenBurns 路径走生产默认（不写视频文件，仅构建 clip 对象树）。"""
        seg = {"text": "今天天气真好。我们去公园玩吧！", "duration": 3.5,
               "words": _WORDS_CONTIGUOUS}
        clip = video._build_segment_clip(seg, 0, str(tmp_path), 320, 180)
        # KenBurns 路径：clip.clips[0] = 动效背景，clips[1:] = 逐句叠加层
        overlays = clip.clips[1:]
        assert len(overlays) == 2
        # 第一句真实结束于 1.2s（字符比例估算会是 3.5*6/13≈1.615，强区分）
        assert overlays[0].duration == pytest.approx(1.2)
        assert overlays[0].start == pytest.approx(0.0)
        # 尾块被 pad_chunk_durations 延长兜住整段：1.8s 真实时长 + 0.5s 尾部静音
        assert overlays[1].start == pytest.approx(1.2)
        assert overlays[1].duration == pytest.approx(2.3)
        assert clip.duration == pytest.approx(3.5)

    def test_video_segment_clip_without_words_still_proportional(self, monkeypatch, tmp_path):
        """无 words 的段：video 侧回退字符比例（旧行为），总和仍被 pad 到段时长"""
        seg = {"text": "今天天气真好。我们去公园玩吧！", "duration": 3.5}
        clip = video._build_segment_clip(seg, 0, str(tmp_path), 320, 180)
        overlays = clip.clips[1:]
        assert len(overlays) == 2
        # 比例路径权重 = 原文字符数（含标点）：3.5 * 7/(7+8) ≈ 1.6333
        # （≠ 真实边界的 1.2，构成对照）
        assert overlays[0].duration == pytest.approx(3.5 * 7 / 15, rel=1e-6)
        assert sum(o.duration for o in overlays) == pytest.approx(3.5)


def _call_real_or_skip(fn, *args, **kwargs):
    """真实 Key 验证辅助：402/quota/billing 类错误说明 Key 有效但无可用额度，
    真实验证仍属待办 → skip（与「缺 Key 自动跳过」同一语义）；
    其余异常照常抛出——那是实现问题，需要修代码而不是跳过。"""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        msg = str(e)
        if "402" in msg or "quota" in msg.lower() or "billing" in msg.lower():
            pytest.skip(f"Key 有效但额度不足（402/quota），真实验证仍待办: {msg[:120]}")
        raise


@pytest.mark.skipif(
    os.getenv("RUN_REAL_KEY_TESTS", "") != "1",
    reason="真实服务验证是显式动作：配好有额度的 Key 后设 RUN_REAL_KEY_TESTS=1 运行（-k RealKey），"
           "常规离线套件不应取决于账户余额",
)
class TestRealKeyTodo:
    """【待办】真实 Key 验证（D2 报告 §遗留）：本机没有任何 API Key，
    以下用例在配好真实 Key 的机器上才会真正执行（出网验证协议实现，
    结果需人工核对）。缺 Key 时自动跳过，不影响离线套件。"""

    def test_real_stepfun_ws(self):
        if not config.STEPFUN_API_KEY:
            pytest.skip("未配置 STEPFUN_API_KEY（真实 Key 验证待办）")
        words = _call_real_or_skip(tts.fetch_stepfun_word_timestamps, "今天天气真好，我们去公园玩吧。")
        assert words is not None, "真实 WS 通道应能取到词级时间戳"
        for w in words:
            assert {"word", "start", "end"} <= set(w)
            assert 0 <= w["start"] <= w["end"]

    def test_real_minimax_subtitle(self):
        if not config.MINIMAX_API_KEY:
            pytest.skip("未配置 MINIMAX_API_KEY（真实 Key 验证待办）")
        words = _call_real_or_skip(tts.fetch_minimax_sentence_timestamps, "今天天气真好，我们去公园玩吧。")
        assert words is not None, "真实 MiniMax 通道应能取到句级时间戳"
        for w in words:
            assert {"word", "start", "end"} <= set(w)
            assert 0 <= w["start"] <= w["end"]

    def test_real_volc_timestamp(self):
        """volc 真实 Key 自验入口（B 队 mustFix：三通道中结构最不确定的一条
        ——with_timestamp 放 request 块、addition 宽松解析均按搜索摘要推断，
        此前没有真实验证入口）。若断言失败：接口拒绝 with_timestamp 或
        addition 结构与推断不符，按真实响应修正 _volc_payload /
        parse_timestamp_file_json（-k RealKey 出网验证）。"""
        if not (config.VOLC_TTS_APPID and config.VOLC_TTS_TOKEN):
            pytest.skip("未配置 VOLC_TTS_APPID / VOLC_TTS_TOKEN（真实 Key 验证待办）")
        words = _call_real_or_skip(tts.fetch_volc_word_timestamps, "今天天气真好，我们去公园玩吧。",
                                    voice=config.VOLC_TTS_VOICE_TYPE)
        assert words is not None, (
            "真实 volc 通道未解析出时间戳：with_timestamp 参数位置或 addition "
            "结构与推断不符（本实现为搜索摘要推断，无官方响应示例），"
            "需按真实响应修正请求结构与解析器")
        for w in words:
            assert {"word", "start", "end"} <= set(w)
            assert 0 <= w["start"] <= w["end"]

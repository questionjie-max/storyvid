"""最小可用的 WebSocket 客户端（RFC 6455 子集，纯标准库，任务 D2）。

StepFun 流式语音合成的词级时间戳走 WebSocket（官方文档：
https://platform.stepfun.com/docs/zh/api-reference/audio/ws-audio，见 R1 §2.6）。
venv 没有、也不引入 websockets / websocket-client 依赖（任务要求用 venv 现有
依赖或标准库），这里用标准库实现一个够用的同步客户端：

- 支持 ws:// 明文与 wss://（TLS，默认 CA 校验）；
- 客户端→服务端帧按 RFC 6455 §5.3 必须掩码（随机 4 字节 key）；
- 自动应答 Ping（回 Pong）、忽略 Pong、聚合分片消息（FIN=0 + CONT）；
- 收到 Close 帧或对端断开时 recv_text 返回 None；
- 握手校验 Sec-WebSocket-Accept（RFC 4.2.2 第 4 步），拒绝非 101 响应。

不支持（本场景用不到）：permessage-deflate 等扩展（握手时不申请）、
服务端掩码帧（RFC 规定服务端不得掩码，但收到时也会解）、流式发送 API。
单帧上限 16MB 护栏，防异常服务端拖垮内存。
"""
import base64
import hashlib
import os
import socket
import ssl
import struct
from urllib.parse import urlsplit

# RFC 6455 固定 GUID，用于计算 Sec-WebSocket-Accept（§1.3）
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

_OP_CONT = 0x0
_OP_TEXT = 0x1
_OP_BIN = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

_MAX_FRAME = 16 * 1024 * 1024  # 单帧/握手响应护栏：16MB


class WebSocketError(Exception):
    """握手失败 / 协议违规 / 连接异常断开"""


def _parse_url(url: str) -> tuple[str, int, str, bool]:
    """拆 ws URL → (host, port, path_with_query, use_tls)；非法 scheme 报错"""
    parts = urlsplit(url)
    if parts.scheme not in ("ws", "wss"):
        raise WebSocketError(f"仅支持 ws:// 或 wss://，收到: {url!r}")
    if not parts.hostname:
        raise WebSocketError(f"URL 缺少主机名: {url!r}")
    use_tls = parts.scheme == "wss"
    port = parts.port or (443 if use_tls else 80)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return parts.hostname, port, path, use_tls


class WsClient:
    """同步阻塞式 WebSocket 文本客户端。

    每个实例一条连接，非线程安全（D2 的 StepFun 时间戳通道每段独立建连，
    并行发生在「每段一个客户端」粒度，互不共享）。
    """

    def __init__(self, url: str, headers: dict | None = None, timeout: float = 30.0):
        self._timeout = max(float(timeout), 1.0)
        self._leftover = b""  # 握手读取多吞的字节（可能已含首帧数据，须回灌帧读取器）
        host, port, path, use_tls = _parse_url(url)
        raw = socket.create_connection((host, port), timeout=self._timeout)
        try:
            if use_tls:
                raw = ssl.create_default_context().wrap_socket(
                    raw, server_hostname=host)
            self._sock = raw
            self._handshake(host, port, path, headers or {})
            # 握手后统一套超时：之后每次 recv 都受保护，不会无限阻塞
            self._sock.settimeout(self._timeout)
        except Exception:
            try:
                raw.close()
            except OSError:
                pass
            raise

    # ---------- 握手 ----------

    def _handshake(self, host: str, port: int, path: str, headers: dict) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        host_header = host if port in (80, 443) else f"{host}:{port}"
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host_header}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        lines.extend(f"{k}: {v}" for k, v in headers.items())
        self._send_raw(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))

        status_line, resp_headers = self._read_http_head()
        if " 101" not in f" {status_line}":
            raise WebSocketError(f"WebSocket 握手被拒绝: {status_line}")
        accept = resp_headers.get("sec-websocket-accept", "")
        expected = base64.b64encode(
            hashlib.sha1((key + _GUID).encode("ascii")).digest()
        ).decode("ascii")
        if accept.strip() != expected:
            raise WebSocketError(
                "WebSocket 握手校验失败（Sec-WebSocket-Accept 不匹配）")

    def _read_http_head(self) -> tuple[str, dict]:
        """读到握手响应头结束（\\r\\n\\r\\n），返回 (状态行, {小写header: 值})。

        服务端常把 101 响应与首个事件帧合并在同一 TCP 段里发——多读出的
        字节绝不能丢弃，存入 _leftover 供 _read_exact 回灌（否则首个事件
        帧被吞，客户端会干等到超时）。
        """
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WebSocketError("握手阶段连接被关闭")
            buf += chunk
            if len(buf) > _MAX_FRAME:
                raise WebSocketError("握手响应过大")
        head, _, self._leftover = buf.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        return lines[0], headers

    # ---------- 帧收发 ----------

    def _send_raw(self, data: bytes) -> None:
        self._sock.sendall(data)

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        """发送一帧（FIN=1、客户端必须掩码）。本客户端不做消息分片发送。"""
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._send_raw(bytes(header) + mask + masked)

    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        # 先消费握手阶段多读的字节（详见 _read_http_head 注释）
        if self._leftover:
            take = min(len(self._leftover), n)
            buf += self._leftover[:take]
            self._leftover = self._leftover[take:]
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise WebSocketError("连接被对端关闭")
            buf += chunk
        return bytes(buf)

    def send_text(self, text: str) -> None:
        self._send_frame(_OP_TEXT, text.encode("utf-8"))

    def recv_text(self) -> "str | None":
        """收下一条完整文本消息；对端发起关闭时返回 None。

        - Ping 帧自动回 Pong 后继续收；Pong 忽略；
        - 分片消息（FIN=0 + CONT）聚合到 FIN 才返回；
        - 二进制帧也按 UTF-8 解码（TTS 服务端事件均为 JSON 文本）。
        """
        buffer = b""
        while True:
            b1, b2 = self._read_exact(2)
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            length = b2 & 0x7F
            if length == 126:
                (length,) = struct.unpack(">H", self._read_exact(2))
            elif length == 127:
                (length,) = struct.unpack(">Q", self._read_exact(8))
            if length > _MAX_FRAME:
                raise WebSocketError(f"单帧超长（{length} 字节），疑似协议异常")
            if masked:  # RFC 规定服务端不该掩码；宽容处理：照常解
                mask = self._read_exact(4)
                payload = bytes(
                    b ^ mask[i % 4] for i, b in enumerate(self._read_exact(length)))
            else:
                payload = self._read_exact(length)

            if opcode == _OP_CLOSE:
                try:
                    self._send_frame(_OP_CLOSE, payload[:2])  # 回显关闭码
                except OSError:
                    pass
                return None
            if opcode == _OP_PING:
                self._send_frame(_OP_PONG, payload)
                continue
            if opcode == _OP_PONG:
                continue
            if opcode in (_OP_TEXT, _OP_BIN, _OP_CONT):
                buffer += payload
                if fin:
                    try:
                        return buffer.decode("utf-8")
                    except UnicodeDecodeError as e:
                        raise WebSocketError(f"消息不是合法 UTF-8: {e}") from None
                continue
            raise WebSocketError(f"未知的 WebSocket opcode: {opcode}")

    def close(self) -> None:
        """尽力发 Close 帧后关闭 socket（重复调用安全，失败静默）。"""
        try:
            self._send_frame(_OP_CLOSE, b"\x03\xe8")  # 1000 正常关闭
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

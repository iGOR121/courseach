"""TCP transport for chat messages.

Messages are newline-delimited JSON objects, UTF-8 encoded. A sender opens a
short-lived TCP connection, writes a ``hello`` frame, one or more ``chat``
frames and a ``bye`` frame, then closes. The receiver runs a small threaded
server and hands every parsed frame to a callback.

Keeping the connection short-lived makes the app stateless and robust: a
dropped connection never leaves a half-open chat behind.
"""

from __future__ import annotations

import json
import socket
import socketserver
import time
from typing import Callable, Dict, Optional

# Types of frame that travel over TCP.
TYPE_HELLO = "hello"
TYPE_CHAT = "chat"
TYPE_BYE = "bye"


class ChatServer(socketserver.ThreadingTCPServer):
    """Threaded TCP server that decodes frames and forwards them."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler, local_id: str, local_nick: str) -> None:
        self.local_id = local_id
        self.local_nick = local_nick
        self.on_message: Optional[Callable[[dict], None]] = None
        super().__init__(address, handler)

    @property
    def port(self) -> int:
        return self.server_address[1]


class _ChatHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.connection.settimeout(30.0)
        try:
            for raw in self.rfile:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                # Let the app know where the message came from.
                if isinstance(msg, dict):
                    msg["_addr"] = self.client_address[0]
                callback = self.server.on_message
                if callback is not None:
                    try:
                        callback(msg)
                    except Exception:
                        # A bad message must never take down the listener.
                        pass
        except OSError:
            pass


def encode_frames(local_id: str, local_nick: str, text: str) -> bytes:
    """Build the bytes for a single chat delivery."""
    frames = [
        {"type": TYPE_HELLO, "id": local_id, "nick": local_nick},
        {
            "type": TYPE_CHAT,
            "id": local_id,
            "nick": local_nick,
            "text": text,
            "ts": time.time(),
        },
        {"type": TYPE_BYE},
    ]
    return b"".join(json.dumps(f).encode("utf-8") + b"\n" for f in frames)


def send_chat(
    host: str,
    port: int,
    local_id: str,
    local_nick: str,
    text: str,
    timeout: float = 5.0,
) -> None:
    """Deliver one message to a peer. Raises OSError if it cannot connect."""
    payload = encode_frames(local_id, local_nick, text)
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(payload)

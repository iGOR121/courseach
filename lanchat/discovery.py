"""Peer discovery over UDP multicast.

Every running instance joins the same multicast group and periodically
announces itself. An announcement contains the sender's nickname and the TCP
port it is listening on, so any peer that sees the announcement knows where to
open a chat connection.

Multicast is used instead of plain broadcast because it also loops back to
other instances running on the *same* machine. That makes it possible to test
the app with two terminals on one computer.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# Bumped if the wire format ever changes.
MAGIC = "lanchat/1"

# Administratively scoped multicast address (RFC 2365) + a private port.
DEFAULT_GROUP = "239.255.42.99"
DEFAULT_PORT = 47820

# Announcements also go to the limited broadcast address. Some networks filter
# multicast but pass broadcast, and vice versa, so we use both.
BROADCAST_ADDR = "255.255.255.255"

# How often we tell the network we exist, and when we consider a peer gone.
ANNOUNCE_INTERVAL = 3.0
PEER_TIMEOUT = 12.0


@dataclass
class Peer:
    """A chat partner we know how to reach."""

    peer_id: str
    nick: str
    host: str
    tcp_port: int
    manual: bool = False
    last_seen: float = field(default_factory=time.time)

    @property
    def address(self) -> str:
        return f"{self.host}:{self.tcp_port}"

    def __str__(self) -> str:
        suffix = " (manual)" if self.manual else ""
        return f"{self.nick} @ {self.address}{suffix}"


class Discovery:
    """Announces this node and tracks the peers that announce themselves."""

    def __init__(
        self,
        peer_id: str,
        nick_provider: Callable[[], str],
        tcp_port: int,
        group: str = DEFAULT_GROUP,
        port: int = DEFAULT_PORT,
        on_change: Optional[Callable[["Discovery"], None]] = None,
    ) -> None:
        self.peer_id = peer_id
        self.tcp_port = tcp_port
        self.group = group
        self.port = port
        self.on_change = on_change

        self._nick_provider = nick_provider
        self._peers: Dict[str, Peer] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._sock: Optional[socket.socket] = None
        self._threads: List[threading.Thread] = []

        # Simple counters, handy for diagnosing discovery problems.
        self.sent = 0
        self.received = 0
        self.replied = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "Discovery":
        self._sock = self._open_socket()
        self._sock.settimeout(1.0)

        for target, name in (
            (self._recv_loop, "lanchat-discovery-rx"),
            (self._announce_loop, "lanchat-discovery-tx"),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()

        sock = self._sock
        if sock is not None:
            # Tell everyone we are leaving so they can drop us immediately.
            bye = {"magic": MAGIC, "type": "bye", "id": self.peer_id}
            for address in self._targets():
                try:
                    self._send_to(sock, bye, address)
                except OSError:
                    pass
            try:
                sock.close()
            except OSError:
                pass

        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads.clear()

    # -- socket setup ------------------------------------------------------

    def _open_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        # Both options are needed so several instances can share the port,
        # which is what enables same-machine testing.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass

        sock.bind(("", self.port))

        mreq = struct.pack("4s4s", socket.inet_aton(self.group), socket.inet_aton("0.0.0.0"))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        try:
            # TTL 2 keeps announcements on the local network / one hop away.
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        except OSError:
            pass
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        return sock

    # -- sending -----------------------------------------------------------

    def _hello_payload(self) -> dict:
        return {
            "magic": MAGIC,
            "type": "hello",
            "id": self.peer_id,
            "nick": self._nick_provider(),
            "tcp_port": self.tcp_port,
        }

    def _send_to(self, sock: socket.socket, payload: dict, address: str) -> None:
        data = json.dumps(payload).encode("utf-8")
        sock.sendto(data, (address, self.port))

    def _targets(self) -> List[str]:
        """Where announcements are sent: multicast plus broadcast."""
        return [self.group, BROADCAST_ADDR]

    def announce(self) -> None:
        sock = self._sock
        if sock is None:
            return
        payload = self._hello_payload()
        for address in self._targets():
            try:
                self._send_to(sock, payload, address)
                self.sent += 1
            except OSError:
                # Broadcast is unavailable on some networks; multicast may
                # still work, so treat this as best-effort.
                pass

    def _reply_to(self, host: str) -> None:
        """Send a hello straight to one host.

        This is the important trick: a device that cannot receive multicast
        (common on Wi-Fi) will still receive a direct unicast packet. When we
        first hear a new peer we answer it directly so it can discover us too.
        """
        sock = self._sock
        if sock is None:
            return
        try:
            self._send_to(sock, self._hello_payload(), host)
            self.replied += 1
        except OSError:
            pass

    # -- receiving ---------------------------------------------------------

    def _recv_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(4096)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break  # socket closed during shutdown
            self._handle(data, addr)

    def _handle(self, data: bytes, addr) -> None:
        try:
            msg = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return

        if msg.get("magic") != MAGIC:
            return
        peer_id = msg.get("id")
        if not peer_id or peer_id == self.peer_id:
            return  # malformed, or our own looped-back announcement

        self.received += 1
        kind = msg.get("type")
        notify = False
        new_peer_host: Optional[str] = None

        with self._lock:
            if kind == "bye":
                notify = self._peers.pop(peer_id, None) is not None
            elif kind == "hello":
                try:
                    tcp_port = int(msg.get("tcp_port") or 0)
                except (TypeError, ValueError):
                    return
                if tcp_port <= 0:
                    return

                nick = str(msg.get("nick") or "?")
                peer = self._peers.get(peer_id)
                if peer is None:
                    self._peers[peer_id] = Peer(peer_id, nick, addr[0], tcp_port)
                    notify = True
                    new_peer_host = addr[0]
                elif (peer.nick, peer.host, peer.tcp_port) != (nick, addr[0], tcp_port):
                    # Nickname or port changed - refresh it.
                    peer.nick, peer.host, peer.tcp_port = nick, addr[0], tcp_port
                    notify = True
                else:
                    peer.last_seen = time.time()

        # Answer a brand new peer directly, in case it cannot hear multicast.
        if new_peer_host is not None:
            self._reply_to(new_peer_host)

        if notify and self.on_change is not None:
            self.on_change(self)

    def _announce_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.announce()
                self.prune()
            except OSError:
                if self._stop.is_set():
                    break
            self._stop.wait(ANNOUNCE_INTERVAL)

    # -- peer bookkeeping --------------------------------------------------

    def prune(self) -> None:
        """Drop peers we have not heard from in a while."""
        cutoff = time.time() - PEER_TIMEOUT
        removed = False
        with self._lock:
            for peer_id in [pid for pid, p in self._peers.items() if p.last_seen < cutoff]:
                del self._peers[peer_id]
                removed = True
        if removed and self.on_change is not None:
            self.on_change(self)

    def snapshot(self) -> List[Peer]:
        with self._lock:
            return sorted(self._peers.values(), key=lambda p: (p.nick.lower(), p.host))

    def get(self, peer_id: str) -> Optional[Peer]:
        with self._lock:
            return self._peers.get(peer_id)

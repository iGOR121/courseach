"""The chat application: ties discovery and transport together, plus a small
terminal user interface.
"""

from __future__ import annotations

import argparse
import getpass
import os
import socket
import sys
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional

from .discovery import DEFAULT_GROUP, DEFAULT_PORT, Discovery, Peer
from .transport import ChatServer, _ChatHandler, send_chat, TYPE_CHAT


class App:
    """A single chat node. Also usable programmatically (see selftest.py)."""

    def __init__(
        self,
        nick: str,
        group: str = DEFAULT_GROUP,
        discovery_port: int = DEFAULT_PORT,
        tcp_port: int = 0,
        on_message: Optional[Callable[[dict], None]] = None,
        on_peers: Optional[Callable[[], None]] = None,
    ) -> None:
        self.peer_id = uuid.uuid4().hex[:8]
        self.nick = nick

        self._on_message = on_message
        self._on_peers = on_peers
        self._manual: Dict[str, Peer] = {}

        # Bind the TCP server first so we know our real port before announcing.
        self.server = ChatServer(("", tcp_port), _ChatHandler, self.peer_id, nick)
        self.server.on_message = self._handle_frame

        self.discovery = Discovery(
            self.peer_id,
            lambda: self.nick,
            self.server.port,
            group=group,
            port=discovery_port,
            on_change=lambda _d: self._notify_peers(),
        )
        self._server_thread: Optional[threading.Thread] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "App":
        self._server_thread = threading.Thread(
            target=self.server.serve_forever, name="lanchat-tcp", daemon=True
        )
        self._server_thread.start()
        self.discovery.start()
        return self

    def stop(self) -> None:
        try:
            self.discovery.stop()
        except Exception:
            pass
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass

    @property
    def port(self) -> int:
        return self.server.port

    # -- peers -------------------------------------------------------------

    def peers(self) -> List[Peer]:
        """All reachable peers: auto-discovered first, then manual ones."""
        discovered = self.discovery.snapshot()
        manual = list(self._manual.values())
        return discovered + manual

    def get_peer(self, peer_id: str) -> Optional[Peer]:
        return self.discovery.get(peer_id) or self._manual.get(peer_id)

    def add_manual(self, host: str, port: int, nick: Optional[str] = None) -> Peer:
        """Register a peer by address, for networks that block discovery."""
        peer_id = f"manual:{host}:{port}"
        peer = Peer(peer_id, nick or host, host, int(port), manual=True)
        self._manual[peer_id] = peer
        self._notify_peers()
        return peer

    def resolve(self, query: str) -> Optional[Peer]:
        """Turn a nickname, nickname prefix, index or address into a peer."""
        peers = self.peers()
        query = query.strip()

        # By number, as shown by /peers.
        if query.isdigit():
            index = int(query) - 1
            if 0 <= index < len(peers):
                return peers[index]
            return None

        lowered = query.lower()
        exact = [p for p in peers if p.nick.lower() == lowered]
        if len(exact) == 1:
            return exact[0]
        prefix = [p for p in peers if p.nick.lower().startswith(lowered)]
        if len(prefix) == 1:
            return prefix[0]
        host_match = [p for p in peers if p.address.lower() == lowered]
        if len(host_match) == 1:
            return host_match[0]
        return None

    # -- messaging ---------------------------------------------------------

    def send(self, peer: Peer, text: str) -> None:
        """Send one message. Raises OSError if the peer is unreachable."""
        send_chat(peer.host, peer.tcp_port, self.peer_id, self.nick, text)

    def _handle_frame(self, frame: dict) -> None:
        if frame.get("type") != TYPE_CHAT:
            return
        if frame.get("id") == self.peer_id:
            return
        if self._on_message is not None:
            self._on_message(frame)

    def _notify_peers(self) -> None:
        if self._on_peers is not None:
            self._on_peers()


# ---------------------------------------------------------------------------
# Terminal user interface
# ---------------------------------------------------------------------------

class Colors:
    """Tiny ANSI helper that degrades to plain text when colour is off."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def prompt(self, text: str) -> str:
        return self._wrap("1;36", text)


BANNER = r"""
  _                _           _
 | |    __ _ _ __ | |__   __ _| |_
 | |   / _` | '_ \| '_ \ / _` | __|
 | |__| (_| | | | | | | | (_| | |_
 |_____\__,_|_| |_|_| |_|\__,_|\__|   local-network chat
"""

HELP = """\
Commands:
  /peers              list peers (numbered)          /nick <name>   change your nickname
  /to <peer>          pick who you are talking to     /add <host> <port> [nick]  add peer by address
  /msg <peer> <text>  send once without switching     /clear         clear the screen
  /me <text>          send an action-style message    /help          show this help
  /quit               leave
Anything you type without a leading "/" goes to the current target.\
"""


def _default_nick() -> str:
    for getter in (lambda: getpass.getuser(), lambda: socket.gethostname()):
        try:
            value = getter()
            if value:
                return value
        except Exception:
            continue
    return "anon"


def cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lanchat",
        description="Chat with other devices on the same local network.",
    )
    parser.add_argument("-n", "--nick", default=_default_nick(), help="nickname (default: %(default)s)")
    parser.add_argument("--group", default=DEFAULT_GROUP, help="multicast group (default: %(default)s)")
    parser.add_argument("--discovery-port", type=int, default=DEFAULT_PORT, help="discovery port (default: %(default)s)")
    parser.add_argument("--tcp-port", type=int, default=0, help="fixed TCP port (default: random)")
    parser.add_argument("--no-color", action="store_true", help="disable coloured output")
    args = parser.parse_args(argv)

    color_enabled = (not args.no_color) and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
    colors = Colors(color_enabled)
    # Cursor control only makes sense on a real terminal; skip it when piping.
    interactive = sys.stdout.isatty()
    out_lock = threading.Lock()
    state: Dict[str, object] = {"target": None, "known": set()}

    def print_locked(text: str) -> None:
        with out_lock:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

    def clear_line() -> None:
        if interactive:
            sys.stdout.write("\r\033[2K")

    def on_message(frame: dict) -> None:
        nick = str(frame.get("nick") or "?")
        text = str(frame.get("text") or "")
        when = time.strftime("%H:%M:%S", time.localtime(frame.get("ts") or time.time()))
        with out_lock:
            clear_line()
            sys.stdout.write(f"{colors.dim(when)} {colors.bold(colors.cyan(nick))}: {text}\n")
            sys.stdout.write(colors.prompt("> "))
            sys.stdout.flush()

    def on_peers() -> None:
        peers = app.peers()
        ids = {p.peer_id for p in peers}
        known = state["known"]  # type: ignore[assignment]
        joined = ids - known
        left = known - ids
        state["known"] = ids
        if not joined and not left:
            return
        with out_lock:
            for peer_id in joined:
                peer = app.get_peer(peer_id)
                if peer is not None:
                    clear_line()
                    sys.stdout.write(colors.green(f"* {peer.nick} joined ({peer.address})") + "\n")
            for peer_id in left:
                clear_line()
                sys.stdout.write(colors.yellow("* a peer left") + "\n")
            sys.stdout.write(colors.prompt("> "))
            sys.stdout.flush()

    app = App(
        nick=args.nick,
        group=args.group,
        discovery_port=args.discovery_port,
        tcp_port=args.tcp_port,
        on_message=on_message,
        on_peers=on_peers,
    )

    try:
        app.start()
    except OSError as exc:
        print(colors.red(f"Could not start on the network: {exc}"), file=sys.stderr)
        print("Try a different --discovery-port, or check your firewall.", file=sys.stderr)
        return 1

    def current_target() -> Optional[Peer]:
        target = state["target"]
        if isinstance(target, Peer):
            return target
        peers = app.peers()
        if len(peers) == 1:
            return peers[0]
        return None

    def do_send(text: str, peer: Optional[Peer]) -> None:
        if peer is None:
            print_locked(colors.red("No target. Use /peers then /to <peer>, or /msg <peer> <text>."))
            return
        try:
            app.send(peer, text)
        except OSError as exc:
            print_locked(colors.red(f"Could not reach {peer.nick} ({peer.address}): {exc}"))
            return
        when = time.strftime("%H:%M:%S")
        print_locked(f"{colors.dim(when)} {colors.bold(colors.green('me -> ' + peer.nick))}: {text}")

    def handle_command(line: str) -> bool:
        """Returns False when the user wants to quit."""
        parts = line[1:].split(maxsplit=1)
        command = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

        if command in ("quit", "exit", "q"):
            print_locked(colors.dim("bye"))
            return False
        if command in ("help", "h", "?"):
            print_locked(HELP)
            return True
        if command in ("peers", "list", "who"):
            peers = app.peers()
            if not peers:
                print_locked(colors.dim("No peers yet. Waiting for others to join..."))
            else:
                lines = ["Peers:"]
                for index, peer in enumerate(peers, 1):
                    marker = " <-" if peer is state["target"] else ""
                    lines.append(f"  [{index}] {peer}{marker}")
                print_locked("\n".join(lines))
            return True
        if command == "to":
            if not rest:
                print_locked(colors.red("Usage: /to <peer>"))
                return True
            peer = app.resolve(rest)
            if peer is None:
                print_locked(colors.red(f"No peer matches '{rest}'. Try /peers."))
            else:
                state["target"] = peer
                print_locked(colors.dim(f"Now talking to {peer.nick} ({peer.address})."))
            return True
        if command == "msg":
            bits = rest.split(maxsplit=1)
            if len(bits) < 2:
                print_locked(colors.red("Usage: /msg <peer> <text>"))
                return True
            do_send(bits[1], app.resolve(bits[0]))
            return True
        if command in ("nick", "name"):
            if not rest:
                print_locked(colors.red("Usage: /nick <name>"))
            else:
                app.nick = rest
                app.server.local_nick = rest
                print_locked(colors.dim(f"Nickname is now {rest}."))
            return True
        if command == "me":
            do_send(f"* {app.nick} {rest}", current_target())
            return True
        if command in ("add", "connect"):
            bits = rest.split()
            if len(bits) < 1:
                print_locked(colors.red("Usage: /add <host> [port] (or /add <host:port>)"))
                return True
            host, port, nick = bits[0], 0, None
            if ":" in host and len(bits) == 1:
                host, _, port_str = host.partition(":")
                port = int(port_str) if port_str.isdigit() else 0
            else:
                if len(bits) >= 2 and bits[1].isdigit():
                    port = int(bits[1])
                if len(bits) >= 3:
                    nick = bits[2]
            if not host or not port:
                print_locked(colors.red("Need a host and a port, e.g. /add 192.168.1.20 51234"))
                return True
            peer = app.add_manual(host, port, nick)
            state["target"] = peer
            print_locked(colors.dim(f"Added {peer}. Now talking to it."))
            return True
        if command == "clear":
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
            return True

        print_locked(colors.red(f"Unknown command '/{command}'. Type /help."))
        return True

    print_locked(colors.bold(BANNER))
    print_locked(f"Signed in as {colors.bold(app.nick)}  |  listening on port {colors.bold(str(app.port))}")
    print_locked(colors.dim("Type /help for commands. Peers on your Wi-Fi/LAN appear automatically."))
    print_locked(colors.dim(f"Discovery group {args.group}:{args.discovery_port}"))

    running = True
    try:
        while running:
            try:
                line = input(colors.prompt("> "))
            except (EOFError, KeyboardInterrupt):
                print()
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith("/"):
                running = handle_command(line)
            else:
                do_send(line, current_target())
    finally:
        app.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(cli())

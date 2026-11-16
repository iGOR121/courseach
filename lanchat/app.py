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
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional, Tuple

from .discovery import (
    DEFAULT_GROUP,
    DEFAULT_PORT,
    MAX_NICK_LENGTH,
    Discovery,
    Peer,
)
from .transport import TYPE_CHAT, ChatHandler, ChatServer, send_chat


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
        self.nick = str(nick)[:MAX_NICK_LENGTH]

        self._on_message = on_message
        self._on_peers = on_peers
        self._manual: Dict[str, Peer] = {}
        self._manual_lock = threading.Lock()
        self._started = False

        # Bind the TCP server first so we know our real port before announcing.
        self.server = ChatServer(("", tcp_port), ChatHandler)
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
        if self._started:
            return self
        self._server_thread = threading.Thread(
            target=self.server.serve_forever, name="lanchat-tcp", daemon=True
        )
        self._server_thread.start()
        self._started = True
        try:
            self.discovery.start()
        except Exception:
            self.stop()
            raise
        return self

    def stop(self) -> None:
        try:
            self.discovery.stop()
        except Exception:
            pass
        try:
            if self._started:
                self.server.close_active()
                self.server.shutdown()
        except Exception:
            pass
        try:
            self.server.server_close()
        except Exception:
            pass
        self._started = False

    @property
    def port(self) -> int:
        return self.server.port

    # -- peers -------------------------------------------------------------

    def peers(self) -> List[Peer]:
        """All reachable peers: auto-discovered first, then manual ones.

        A manual entry is hidden once the same address has been auto-discovered,
        so one device does not show up twice.
        """
        discovered = self.discovery.snapshot()
        seen = {(peer.host, peer.tcp_port) for peer in discovered}
        with self._manual_lock:
            manual = [
                replace(peer)
                for peer in self._manual.values()
                if (peer.host, peer.tcp_port) not in seen
            ]
        manual.sort(key=lambda peer: (peer.nick.lower(), peer.host))
        return discovered + manual

    def get_peer(self, peer_id: str) -> Optional[Peer]:
        peer = self.discovery.get(peer_id)
        if peer is not None:
            return peer

        with self._manual_lock:
            manual = self._manual.get(peer_id)
        if manual is None:
            return None

        # Prefer a discovered copy if the same device later became visible.
        for discovered in self.discovery.snapshot():
            if (discovered.host, discovered.tcp_port) == (manual.host, manual.tcp_port):
                return discovered
        return replace(manual)

    def add_manual(self, host: str, port: int, nick: Optional[str] = None) -> Peer:
        """Register a peer by address, for networks that block discovery."""
        host = host.strip()
        if not host:
            raise ValueError("A host is required.")

        try:
            port = int(port)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("The port must be an integer.") from None
        if not 1 <= port <= 65535:
            raise ValueError("The port must be between 1 and 65535.")

        nickname = ((nick or host).strip() or host)[:MAX_NICK_LENGTH]
        peer_id = f"manual:{host}:{port}"
        peer = Peer(peer_id, nickname, host, port, manual=True)
        with self._manual_lock:
            self._manual[peer_id] = peer
        self._notify_peers()
        return replace(peer)

    def resolve(self, query: str) -> Optional[Peer]:
        """Turn a nickname, nickname prefix, index or address into a peer."""
        peers = self.peers()
        query = query.strip()
        if not query:
            return None

        # By number, as shown by /peers.
        if query.isdigit():
            try:
                index = int(query) - 1
            except ValueError:
                return None
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
        callback = self._on_message
        if callback is not None:
            callback(frame)

    def _notify_peers(self) -> None:
        callback = self._on_peers
        if callback is not None:
            callback()


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
  /peers                     list peers (numbered)
  /to <peer>                 choose who plain text messages go to
  /msg <peer> <text>         send one message without switching
  /me <text>                 send an action-style message
  /nick <name>               change your nickname
  /add <host> <port> [nick]  add a peer by address
  /add <host:port> [nick]    same, with the port in the address
  /clear                     clear the screen
  /help                      show this help
  /quit                      leave

A <peer> can be a nickname, a nickname prefix, the number from /peers,
or host:port. If exactly one peer is online, plain text goes to them.
The prompt shows the current target, e.g. [bob]>.\
"""

ADD_USAGE = "Usage: /add <host> <port> [nick] (or /add <host:port> [nick])"


def _safe_text(value: object) -> str:
    """Make untrusted text safe to print in a terminal.

    Control characters, including ANSI escape sequences, are replaced with
    spaces so a peer cannot clear the screen or move the cursor. Lone UTF-16
    surrogates are replaced too, since stdout cannot encode them.
    """
    return "".join(
        " "
        if (
            ord(char) < 32
            or 0x7F <= ord(char) <= 0x9F
            or 0xD800 <= ord(char) <= 0xDFFF
        )
        else char
        for char in str(value)
    )


def _peer_text(peer: Peer) -> str:
    suffix = " (manual)" if peer.manual else ""
    return f"{_safe_text(peer.nick)} @ {_safe_text(peer.address)}{suffix}"


def _format_time(value: object) -> str:
    try:
        timestamp = float(value) if value else time.time()
        return time.strftime("%H:%M:%S", time.localtime(timestamp))
    except (TypeError, ValueError, OverflowError, OSError):
        return time.strftime("%H:%M:%S")


def _parse_add(rest: str) -> Tuple[str, int, Optional[str]]:
    """Parse ``host port [nick]`` or ``host:port [nick]``."""
    if not rest:
        raise ValueError(ADD_USAGE)

    bits = rest.split(maxsplit=1)
    host = bits[0]
    tail = bits[1].strip() if len(bits) > 1 else ""

    if ":" in host:
        host, _, port_text = host.rpartition(":")
        if not host or not port_text.isdigit() or len(port_text) > 5:
            raise ValueError(ADD_USAGE)
        port = int(port_text)
        nick = tail or None
    else:
        parts = rest.split(maxsplit=2)
        if len(parts) < 2 or not parts[1].isdigit() or len(parts[1]) > 5:
            raise ValueError(ADD_USAGE)
        host = parts[0]
        port = int(parts[1])
        nick = parts[2].strip() if len(parts) > 2 else None

    if not 1 <= port <= 65535:
        raise ValueError("The port must be between 1 and 65535.")
    return host, port, nick or None


def _default_nick() -> str:
    for getter in (lambda: getpass.getuser(), lambda: socket.gethostname()):
        try:
            value = getter()
            if value:
                return value
        except Exception:
            continue
    return "anon"


@dataclass
class _UiState:
    """The bits of UI state shared with the discovery callback."""

    target_address: Optional[str] = None
    known: Dict[str, str] = field(default_factory=dict)


def cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lanchat",
        description="Chat with other devices on the same local network.",
    )
    parser.add_argument(
        "-n",
        "--nick",
        default=_default_nick(),
        help=f"nickname, max {MAX_NICK_LENGTH} chars (default: %(default)s)",
    )
    parser.add_argument(
        "--group", default=DEFAULT_GROUP, help="multicast group (default: %(default)s)"
    )
    parser.add_argument(
        "--discovery-port",
        type=int,
        default=DEFAULT_PORT,
        help="discovery port (default: %(default)s)",
    )
    parser.add_argument(
        "--tcp-port", type=int, default=0, help="fixed TCP port (default: random)"
    )
    parser.add_argument(
        "--no-color", action="store_true", help="disable coloured output"
    )
    args = parser.parse_args(argv)

    if not 1 <= args.discovery_port <= 65535:
        parser.error("--discovery-port must be between 1 and 65535")
    if not 0 <= args.tcp_port <= 65535:
        parser.error("--tcp-port must be between 0 and 65535")

    color_enabled = (
        not args.no_color
        and not os.environ.get("NO_COLOR")
        and sys.stdout.isatty()
        and os.environ.get("TERM") != "dumb"
    )
    colors = Colors(color_enabled)
    # Cursor control only makes sense on a real terminal; skip it when piping.
    interactive = sys.stdout.isatty()

    # RLock because callbacks write output and also inspect the current target.
    out_lock = threading.RLock()
    state = _UiState()
    nick = (
        _safe_text(args.nick).strip()
        or _safe_text(_default_nick()).strip()
        or "anon"
    )
    if len(nick) > MAX_NICK_LENGTH:
        parser.error(f"--nick must be at most {MAX_NICK_LENGTH} characters")

    def print_locked(text: str) -> None:
        with out_lock:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

    def clear_line() -> None:
        if interactive:
            sys.stdout.write("\r\033[2K")

    def current_target() -> Optional[Peer]:
        with out_lock:
            peers = app.peers()
            if state.target_address is not None:
                for peer in peers:
                    if peer.address == state.target_address:
                        return peer
                state.target_address = None
            if len(peers) == 1:
                return peers[0]
            return None

    def prompt_text() -> str:
        target = current_target()
        if target is None:
            return colors.prompt("> ")
        return colors.prompt(f"[{_safe_text(target.nick)}]> ")

    def on_message(frame: dict) -> None:
        sender = (
            _safe_text(frame.get("nick") or "?").strip()[:MAX_NICK_LENGTH] or "?"
        )
        text = _safe_text(frame.get("text") or "")
        when = _format_time(frame.get("ts"))
        with out_lock:
            clear_line()
            sys.stdout.write(
                f"{colors.dim(when)} {colors.bold(colors.cyan(sender))}: {text}\n"
            )
            sys.stdout.write(prompt_text())
            sys.stdout.flush()

    def on_peers() -> None:
        with out_lock:
            peers = app.peers()
            current = {peer.address: peer for peer in peers}
            previous = state.known
            joined = [address for address in current if address not in previous]
            left = [address for address in previous if address not in current]
            renamed = [
                address
                for address in current
                if address in previous and previous[address] != current[address].nick
            ]
            state.known = {address: peer.nick for address, peer in current.items()}

            messages: List[str] = []
            for address in joined:
                peer = current[address]
                messages.append(
                    colors.green(
                        f"* {_safe_text(peer.nick)} joined ({_safe_text(address)})"
                    )
                )
            for address in renamed:
                messages.append(
                    colors.cyan(
                        f"* {_safe_text(previous[address])} is now "
                        f"{_safe_text(current[address].nick)}"
                    )
                )
            for address in left:
                messages.append(
                    colors.yellow(f"* {_safe_text(previous[address])} left")
                )

            if state.target_address is not None and state.target_address not in current:
                state.target_address = None
                messages.append(
                    colors.yellow(
                        "* your chat target left; use /to to pick another peer"
                    )
                )

            if not messages:
                return
            clear_line()
            for message in messages:
                sys.stdout.write(message + "\n")
            sys.stdout.write(prompt_text())
            sys.stdout.flush()

    try:
        app = App(
            nick=nick,
            group=args.group,
            discovery_port=args.discovery_port,
            tcp_port=args.tcp_port,
            on_message=on_message,
            on_peers=on_peers,
        )
    except OSError as exc:
        print(
            colors.red(f"Could not open the chat server: {_safe_text(exc)}"),
            file=sys.stderr,
        )
        print("Check the --tcp-port value and your firewall.", file=sys.stderr)
        return 1

    def do_send(text: str, peer: Optional[Peer]) -> None:
        text = _safe_text(text)
        if peer is None:
            print_locked(
                colors.red(
                    "No target. Use /peers then /to <peer>, or /msg <peer> <text>."
                )
            )
            return
        try:
            app.send(peer, text)
        except OSError as exc:
            print_locked(
                colors.red(
                    f"Could not reach {_safe_text(peer.nick)} "
                    f"({_safe_text(peer.address)}): {_safe_text(exc)}"
                )
            )
            return
        when = time.strftime("%H:%M:%S")
        print_locked(
            f"{colors.dim(when)} "
            f"{colors.bold(colors.green('me -> ' + _safe_text(peer.nick)))}: {text}"
        )

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
                target = current_target()
                lines = [f"Peers ({len(peers)}):"]
                for index, peer in enumerate(peers, 1):
                    marker = (
                        "  <- current"
                        if target is not None and peer.address == target.address
                        else ""
                    )
                    lines.append(f"  [{index}] {_peer_text(peer)}{marker}")
                print_locked("\n".join(lines))
            return True
        if command == "to":
            if not rest:
                print_locked(colors.red("Usage: /to <peer>"))
                return True
            peer = app.resolve(rest)
            if peer is None:
                print_locked(
                    colors.red(
                        f"No unique peer matches '{_safe_text(rest)}'. Try /peers."
                    )
                )
            else:
                with out_lock:
                    state.target_address = peer.address
                print_locked(
                    colors.dim(
                        f"Now talking to {_safe_text(peer.nick)} "
                        f"({_safe_text(peer.address)})."
                    )
                )
            return True
        if command == "msg":
            bits = rest.split(maxsplit=1)
            if len(bits) < 2:
                print_locked(colors.red("Usage: /msg <peer> <text>"))
                return True
            peer = app.resolve(bits[0])
            if peer is None:
                print_locked(
                    colors.red(
                        f"No unique peer matches '{_safe_text(bits[0])}'. "
                        "Try /peers."
                    )
                )
            else:
                do_send(bits[1], peer)
            return True
        if command in ("nick", "name"):
            new_nick = _safe_text(rest).strip()
            if not new_nick:
                print_locked(colors.red("Usage: /nick <name>"))
            elif len(new_nick) > MAX_NICK_LENGTH:
                print_locked(
                    colors.red(
                        f"Nicknames can be at most {MAX_NICK_LENGTH} characters."
                    )
                )
            else:
                app.nick = new_nick
                print_locked(colors.dim(f"Nickname is now {new_nick}."))
            return True
        if command == "me":
            if not rest:
                print_locked(colors.red("Usage: /me <text>"))
            else:
                do_send(f"* {app.nick} {rest}", current_target())
            return True
        if command in ("add", "connect"):
            try:
                host, port, peer_nick = _parse_add(rest)
                peer = app.add_manual(host, port, peer_nick)
            except ValueError as exc:
                print_locked(colors.red(str(exc)))
                return True
            with out_lock:
                state.target_address = peer.address
            print_locked(colors.dim(f"Added {_peer_text(peer)}. Now talking to it."))
            return True
        if command == "clear":
            if interactive:
                with out_lock:
                    sys.stdout.write("\033[2J\033[H")
                    sys.stdout.flush()
            return True

        print_locked(
            colors.red(f"Unknown command '/{_safe_text(command)}'. Type /help.")
        )
        return True

    print_locked(colors.bold(BANNER))
    print_locked(
        f"Signed in as {colors.bold(app.nick)}  |  "
        f"listening on port {colors.bold(str(app.port))}"
    )
    print_locked(
        colors.dim(
            "Type /help for commands. Peers on your Wi-Fi/LAN appear automatically."
        )
    )
    print_locked(
        colors.dim(
            f"Discovery group {_safe_text(args.group)}:{args.discovery_port}"
        )
    )

    try:
        app.start()
    except OSError as exc:
        app.stop()
        print(
            colors.red(f"Could not start discovery: {_safe_text(exc)}"),
            file=sys.stderr,
        )
        print(
            "Try a different --discovery-port or --group, and check your firewall.",
            file=sys.stderr,
        )
        return 1

    running = True
    try:
        while running:
            try:
                line = input(prompt_text())
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
    except KeyboardInterrupt:
        print()
    finally:
        app.stop()

    return 0

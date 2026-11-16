"""Tkinter desktop interface for lanchat.

Run it with::

    python3 -m lanchat.gui --nick alice
"""

from __future__ import annotations

import argparse
import getpass
import queue
import socket
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
from tkinter import messagebox, simpledialog, ttk
from typing import Dict, List, Optional, Tuple

from .app import App
from .discovery import DEFAULT_GROUP, DEFAULT_PORT, MAX_NICK_LENGTH, Peer


def _clean_text(value: object, *, multiline: bool = False) -> str:
    """Replace characters that would break a single line of GUI text."""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    allowed = "\n\t" if multiline else ""
    cleaned: List[str] = []
    for char in text:
        code = ord(char)
        if (
            char in allowed
            or not (
                code < 32
                or 0x7F <= code <= 0x9F
                or 0xD800 <= code <= 0xDFFF
            )
        ):
            cleaned.append(char)
        else:
            cleaned.append(" ")
    return "".join(cleaned)


def _nick_text(value: object) -> str:
    return " ".join(_clean_text(value).split())[:MAX_NICK_LENGTH] or "?"


def _format_time(value: object) -> str:
    try:
        timestamp = float(value) if value else time.time()
        return time.strftime("%H:%M:%S", time.localtime(timestamp))
    except (TypeError, ValueError, OverflowError, OSError):
        return time.strftime("%H:%M:%S")


def _default_nick() -> str:
    for getter in (lambda: getpass.getuser(), lambda: socket.gethostname()):
        try:
            value = getter()
            if value:
                return value
        except Exception:
            continue
    return "anon"


class LanChatGUI:
    """Tkinter UI around a single :class:`lanchat.app.App` node."""

    def __init__(
        self,
        root: tk.Tk,
        nick: str,
        group: str = DEFAULT_GROUP,
        discovery_port: int = DEFAULT_PORT,
        tcp_port: int = 0,
    ) -> None:
        self.root = root
        self.group = group
        self.discovery_port = discovery_port

        self._events: queue.Queue = queue.Queue()
        self._sends: queue.Queue = queue.Queue()
        self._known: Dict[str, str] = {}
        self._peer_map: Dict[str, Peer] = {}
        self._selected_address: Optional[str] = None
        self._refreshing = False
        self._closing = False
        self._poll_job: Optional[str] = None

        self.app = App(
            nick=nick,
            group=group,
            discovery_port=discovery_port,
            tcp_port=tcp_port,
            on_message=self._queue_message,
            on_peers=self._queue_peers,
        )

        self._build_ui()
        self._append_system(
            f"Connected as {_nick_text(self.app.nick)}. "
            f"Listening on TCP port {self.app.port}."
        )
        self._append_system(
            f"Discovery: {_clean_text(self.group)}:{self.discovery_port}. "
            "Waiting for peers…"
        )

        try:
            self.app.start()
        except Exception:
            self.app.stop()
            raise

        self._sender_thread = threading.Thread(
            target=self._send_loop, name="lanchat-gui-send", daemon=True
        )
        self._sender_thread.start()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Control-q>", lambda _event: self._on_close())
        self._refresh_peers()
        self._poll_job = self.root.after(100, self._poll_events)
        self.entry.focus_set()
        self.root.after(100, self._focus_entry)

    # -- UI construction ---------------------------------------------------

    def _build_ui(self) -> None:
        self.root.title(f"lanchat — {_nick_text(self.app.nick)}")
        self.root.minsize(760, 500)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)
        self._center_window()

        style = ttk.Style(self.root)
        family = tkfont.nametofont("TkDefaultFont").actual("family")
        style.configure("Title.TLabel", font=(family, 16, "bold"))
        style.configure("Target.TLabel", font=(family, 10, "bold"))
        style.configure("Peers.TLabel", font=(family, 10, "bold"))

        header = ttk.Frame(self.root, padding=(14, 10, 14, 6))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="lanchat", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.nick_var = tk.StringVar(
            value=f"Signed in as {_nick_text(self.app.nick)}"
        )
        ttk.Label(header, textvariable=self.nick_var).grid(
            row=0, column=1, sticky="e", padx=(12, 10)
        )
        ttk.Button(header, text="Change nick…", command=self._change_nick).grid(
            row=0, column=2
        )

        ttk.Separator(self.root, orient="horizontal").grid(
            row=1, column=0, sticky="ew"
        )

        main = ttk.Frame(self.root, padding=(14, 10, 14, 10))
        main.grid(row=2, column=0, sticky="nsew")
        main.columnconfigure(0, weight=0, minsize=290)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        left = ttk.Frame(main)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)

        self.peers_title_var = tk.StringVar(value="Peers (0)")
        ttk.Label(
            left, textvariable=self.peers_title_var, style="Peers.TLabel"
        ).grid(row=0, column=0, sticky="w")

        tree_frame = ttk.Frame(left)
        tree_frame.grid(row=1, column=0, sticky="nsew", pady=(6, 8))
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        self.peer_tree = ttk.Treeview(
            tree_frame,
            columns=("nick", "address"),
            show="headings",
            selectmode="browse",
        )
        self.peer_tree.heading("nick", text="Peer")
        self.peer_tree.heading("address", text="Address")
        self.peer_tree.column("nick", width=105, anchor="w")
        self.peer_tree.column("address", width=165, anchor="w")
        self.peer_tree.grid(row=0, column=0, sticky="nsew")
        peer_scroll = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self.peer_tree.yview
        )
        peer_scroll.grid(row=0, column=1, sticky="ns")
        self.peer_tree.configure(yscrollcommand=peer_scroll.set)
        self.peer_tree.bind("<<TreeviewSelect>>", self._on_peer_selected)
        self.peer_tree.bind("<Double-1>", lambda _event: self.entry.focus_set())

        buttons = ttk.Frame(left)
        buttons.grid(row=2, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Add peer…", command=self._show_add_peer).grid(
            row=0, column=0, sticky="ew"
        )

        right = ttk.Frame(main)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        self.target_var = tk.StringVar(value="No peers yet — waiting for devices…")
        ttk.Label(
            right, textvariable=self.target_var, style="Target.TLabel"
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))

        chat_frame = ttk.Frame(right)
        chat_frame.grid(row=1, column=0, sticky="nsew")
        chat_frame.columnconfigure(0, weight=1)
        chat_frame.rowconfigure(0, weight=1)

        self.chat = tk.Text(
            chat_frame,
            wrap="word",
            state="disabled",
            height=18,
            padx=10,
            pady=8,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground="#c8c8c8",
            highlightcolor="#1f6feb",
        )
        self.chat.grid(row=0, column=0, sticky="nsew")
        chat_scroll = ttk.Scrollbar(
            chat_frame, orient="vertical", command=self.chat.yview
        )
        chat_scroll.grid(row=0, column=1, sticky="ns")
        self.chat.configure(yscrollcommand=chat_scroll.set)

        text_font = tkfont.nametofont("TkTextFont")
        self.chat.configure(font=text_font)
        self._bold_font = text_font.copy()
        self._bold_font.configure(weight="bold")
        self._italic_font = text_font.copy()
        self._italic_font.configure(slant="italic")
        self.chat.tag_configure("time", foreground="#777777")
        self.chat.tag_configure(
            "nick", foreground="#1f6feb", font=self._bold_font
        )
        self.chat.tag_configure("out", foreground="#1a7f37", font=self._bold_font)
        self.chat.tag_configure(
            "system", foreground="#777777", font=self._italic_font, spacing1=2
        )
        self.chat.tag_configure(
            "error", foreground="#b00020", font=self._bold_font
        )

        input_frame = ttk.Frame(right)
        input_frame.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        input_frame.columnconfigure(0, weight=1)
        self.entry = ttk.Entry(input_frame, font=text_font)
        self.entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.entry.bind("<Return>", self._on_entry_return)
        self.entry.bind("<KP_Enter>", self._on_entry_return)
        self.send_button = ttk.Button(
            input_frame, text="Send", command=self._send_current, state="disabled"
        )
        self.send_button.grid(row=0, column=1)

        status_frame = ttk.Frame(self.root, padding=(14, 4, 14, 8))
        status_frame.grid(row=3, column=0, sticky="ew")
        status_frame.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="Starting…")
        ttk.Label(status_frame, textvariable=self.status_var, anchor="w").grid(
            row=0, column=0, sticky="ew"
        )

        self._build_menu()

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Add peer…", command=self._show_add_peer)
        file_menu.add_command(
            label="Change nickname…", command=self._change_nick
        )
        file_menu.add_separator()
        file_menu.add_command(
            label="Quit", command=self._on_close, accelerator="Ctrl+Q"
        )
        menubar.add_cascade(label="File", menu=file_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="About lanchat", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.configure(menu=menubar)

    def _center_window(self) -> None:
        width, height = 940, 640
        self.root.update_idletasks()
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 3)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _focus_entry(self) -> None:
        if not self._closing:
            self.entry.focus_set()

    # -- callbacks from network threads -----------------------------------

    def _queue_message(self, frame: dict) -> None:
        self._events.put(("message", frame))

    def _queue_peers(self) -> None:
        self._events.put(("peers", None))

    def _send_loop(self) -> None:
        while True:
            item = self._sends.get()
            if item is None:
                return
            peer, text = item
            try:
                self.app.send(peer, text)
            except Exception as exc:
                self._events.put(("send_error", (peer, str(exc))))
            else:
                self._events.put(("sent", (peer, text)))

    def _poll_events(self) -> None:
        self._poll_job = None
        if self._closing:
            return
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "message":
                    self._show_message(payload)
                elif kind == "peers":
                    self._refresh_peers()
                elif kind == "sent":
                    peer, text = payload
                    self._show_sent(peer, text)
                elif kind == "send_error":
                    peer, error = payload
                    self._show_send_error(peer, error)
        except queue.Empty:
            pass
        except Exception:
            # Keep the UI alive even if one event could not be displayed.
            traceback.print_exc()
        if not self._closing:
            self._poll_job = self.root.after(100, self._poll_events)

    # -- peer list ---------------------------------------------------------

    def _refresh_peers(self) -> None:
        peers = self.app.peers()
        current = {peer.address: peer for peer in peers}
        previous = self._known
        joined = [address for address in current if address not in previous]
        left = [address for address in previous if address not in current]
        renamed = [
            address
            for address in current
            if address in previous and previous[address] != current[address].nick
        ]
        self._known = {address: peer.nick for address, peer in current.items()}
        self._peer_map = current

        for address in joined:
            peer = current[address]
            self._append_system(
                f"{_nick_text(peer.nick)} joined ({_clean_text(address)})"
            )
        for address in renamed:
            self._append_system(
                f"{_nick_text(previous[address])} is now "
                f"{_nick_text(current[address].nick)}"
            )
        for address in left:
            self._append_system(f"{_nick_text(previous[address])} left")

        if self._selected_address is not None and self._selected_address not in current:
            self._selected_address = None
            self._append_system(
                "Your chat target left. Select another peer.", error=True
            )

        self._refreshing = True
        try:
            children = self.peer_tree.get_children()
            if children:
                self.peer_tree.delete(*children)
            for peer in peers:
                label = _nick_text(peer.nick)
                if peer.manual:
                    label += " (manual)"
                self.peer_tree.insert(
                    "",
                    "end",
                    iid=peer.address,
                    values=(label, _clean_text(peer.address)),
                )
        finally:
            self._refreshing = False

        if self._selected_address in current:
            self.peer_tree.selection_set(self._selected_address)
            self.peer_tree.focus(self._selected_address)
            self.peer_tree.see(self._selected_address)
        elif len(peers) == 1:
            self._selected_address = peers[0].address
            self.peer_tree.selection_set(self._selected_address)
            self.peer_tree.focus(self._selected_address)
            self.peer_tree.see(self._selected_address)
        else:
            self._selected_address = None
            selection = self.peer_tree.selection()
            if selection:
                self.peer_tree.selection_remove(*selection)

        self.peers_title_var.set(f"Peers ({len(peers)})")
        self._update_target_ui()
        self._update_status(len(peers))

    def _on_peer_selected(self, _event=None) -> None:
        if self._refreshing:
            return
        selection = self.peer_tree.selection()
        self._selected_address = selection[0] if selection else None
        self._update_target_ui()
        if selection and self.root.grab_current() is None:
            self.entry.focus_set()

    def _selected_peer(self) -> Optional[Peer]:
        if self._selected_address is None:
            return None
        return self._peer_map.get(self._selected_address)

    def _update_target_ui(self) -> None:
        peer = self._selected_peer()
        if peer is not None:
            self.target_var.set(
                f"To: {_nick_text(peer.nick)} ({_clean_text(peer.address)})"
            )
            self.send_button.state(["!disabled"])
        elif self._peer_map:
            self.target_var.set("Select a peer on the left")
            self.send_button.state(["disabled"])
        else:
            self.target_var.set("No peers yet — waiting for devices on the LAN")
            self.send_button.state(["disabled"])

    def _update_status(self, peer_count: Optional[int] = None) -> None:
        if peer_count is None:
            peer_count = len(self._peer_map)
        peer_word = "peer" if peer_count == 1 else "peers"
        self.status_var.set(
            f"Listening on TCP port {self.app.port}  |  "
            f"discovery {_clean_text(self.group)}:{self.discovery_port}  |  "
            f"{peer_count} {peer_word}"
        )

    def _update_header(self) -> None:
        self.nick_var.set(f"Signed in as {_nick_text(self.app.nick)}")
        self.root.title(f"lanchat — {_nick_text(self.app.nick)}")
        self._update_status()

    # -- sending and displaying messages ----------------------------------

    def _on_entry_return(self, _event=None):
        self._send_current()
        return "break"

    def _send_current(self) -> None:
        peer = self._selected_peer()
        text = self.entry.get().strip()
        if peer is None:
            self._append_system("Select a peer before sending a message.", error=True)
            return
        if not text:
            return
        self._sends.put((peer, text))
        self.entry.delete(0, tk.END)
        self.entry.focus_set()

    def _show_message(self, frame: dict) -> None:
        sender = _nick_text(frame.get("nick") or "?")
        text = _clean_text(frame.get("text") or "", multiline=True)
        when = _format_time(frame.get("ts"))
        self._append(when + " ", ("time",))
        self._append(sender + ": ", ("nick",))
        self._append(text + "\n", ("text",))

    def _show_sent(self, peer: Peer, text: str) -> None:
        when = time.strftime("%H:%M:%S")
        self._append(when + " ", ("time",))
        self._append(f"me -> {_nick_text(peer.nick)}: ", ("out",))
        self._append(_clean_text(text, multiline=True) + "\n", ("text",))

    def _show_send_error(self, peer: Peer, error: str) -> None:
        self._append_system(
            f"Could not reach {_nick_text(peer.nick)} "
            f"({_clean_text(peer.address)}): {_clean_text(error)}",
            error=True,
        )

    def _append(self, text: str, tags: Tuple[str, ...]) -> None:
        self.chat.configure(state="normal")
        self.chat.insert("end", text, tags)
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def _append_system(self, text: str, *, error: bool = False) -> None:
        prefix = "! " if error else "* "
        tags: Tuple[str, ...] = ("error",) if error else ("system",)
        self._append(prefix + _clean_text(text) + "\n", tags)

    # -- dialogs -----------------------------------------------------------

    def _show_add_peer(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Add peer")
        dialog.transient(self.root)
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(1, weight=1)

        host_var = tk.StringVar()
        port_var = tk.StringVar()
        nick_var = tk.StringVar()
        fields = (
            ("Host", host_var),
            ("Port", port_var),
            ("Nickname (optional)", nick_var),
        )
        entries: List[ttk.Entry] = []
        for row, (label, var) in enumerate(fields):
            ttk.Label(frame, text=label).grid(
                row=row, column=0, sticky="w", pady=(0, 8), padx=(0, 10)
            )
            entry = ttk.Entry(frame, textvariable=var, width=28)
            entry.grid(row=row, column=1, sticky="ew", pady=(0, 8))
            entries.append(entry)

        buttons = ttk.Frame(frame)
        buttons.grid(
            row=len(fields), column=0, columnspan=2, sticky="e", pady=(4, 0)
        )

        def add() -> None:
            host = host_var.get().strip()
            port_text = port_var.get().strip()
            try:
                port = int(port_text)
            except ValueError:
                messagebox.showerror(
                    "Add peer", "Port must be a number.", parent=dialog
                )
                return
            try:
                peer = self.app.add_manual(
                    host, port, nick_var.get().strip() or None
                )
            except ValueError as exc:
                messagebox.showerror("Add peer", str(exc), parent=dialog)
                return
            self._selected_address = peer.address
            self._refresh_peers()
            dialog.destroy()
            self.entry.focus_set()

        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(
            side="right"
        )
        ttk.Button(buttons, text="Add", command=add).pack(
            side="right", padx=(0, 8)
        )
        dialog.bind("<Return>", lambda _event: add())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        entries[0].focus_set()
        self._center_dialog(dialog)
        try:
            dialog.grab_set()
        except tk.TclError:
            pass

    def _change_nick(self) -> None:
        new_nick = simpledialog.askstring(
            "Change nickname",
            "New nickname:",
            initialvalue=self.app.nick,
            parent=self.root,
        )
        if new_nick is None:
            return
        new_nick = " ".join(_clean_text(new_nick).split())
        if not new_nick:
            messagebox.showerror(
                "Change nickname",
                "Nickname cannot be empty.",
                parent=self.root,
            )
            return
        if len(new_nick) > MAX_NICK_LENGTH:
            messagebox.showerror(
                "Change nickname",
                f"Nickname can be at most {MAX_NICK_LENGTH} characters.",
                parent=self.root,
            )
            return
        self.app.nick = new_nick
        self._update_header()
        self._append_system(f"You are now {new_nick}.")

    def _show_about(self) -> None:
        messagebox.showinfo(
            "About lanchat",
            "lanchat\n\n"
            "Peer-to-peer chat for the local network.\n"
            "Devices find each other with UDP multicast and exchange\n"
            "messages directly over TCP.",
            parent=self.root,
        )

    def _center_dialog(self, dialog: tk.Toplevel) -> None:
        dialog.update_idletasks()
        width = dialog.winfo_width()
        height = dialog.winfo_height()
        x = self.root.winfo_rootx() + max(
            0, (self.root.winfo_width() - width) // 2
        )
        y = self.root.winfo_rooty() + max(
            0, (self.root.winfo_height() - height) // 3
        )
        dialog.geometry(f"+{x}+{y}")

    # -- shutdown ----------------------------------------------------------

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None
        self._sends.put(None)
        try:
            self.status_var.set("Shutting down…")
            self.root.update_idletasks()
        except tk.TclError:
            pass
        try:
            self.app.stop()
        finally:
            try:
                self.root.destroy()
            except tk.TclError:
                pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lanchat-gui",
        description="Tkinter interface for chatting on the local network.",
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
    args = parser.parse_args(argv)

    if not 1 <= args.discovery_port <= 65535:
        parser.error("--discovery-port must be between 1 and 65535")
    if not 0 <= args.tcp_port <= 65535:
        parser.error("--tcp-port must be between 0 and 65535")

    nick = " ".join(_clean_text(args.nick).split())
    if not nick:
        nick = " ".join(_clean_text(_default_nick()).split())
    if not nick:
        nick = "anon"
    if len(nick) > MAX_NICK_LENGTH:
        parser.error(f"--nick must be at most {MAX_NICK_LENGTH} characters")

    try:
        root = tk.Tk(className="lanchat")
    except tk.TclError as exc:
        print(f"Could not open a window: {exc}", file=sys.stderr)
        print("Tkinter needs a graphical display.", file=sys.stderr)
        return 1

    root.withdraw()
    try:
        gui = LanChatGUI(
            root,
            nick=nick,
            group=args.group,
            discovery_port=args.discovery_port,
            tcp_port=args.tcp_port,
        )
    except OSError as exc:
        messagebox.showerror(
            "lanchat",
            f"Could not start the chat:\n\n{_clean_text(exc)}",
            parent=root,
        )
        root.destroy()
        return 1
    except Exception:
        root.destroy()
        raise

    root.deiconify()
    try:
        root.mainloop()
    except KeyboardInterrupt:
        gui._on_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

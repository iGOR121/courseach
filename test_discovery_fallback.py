#!/usr/bin/env python3
"""Test the discovery fallback for devices that cannot receive multicast.

Some Wi-Fi networks deliver multicast in one direction only, which is why a
phone can see a laptop but the laptop cannot see the phone (or vice versa).

This simulates a "phone" that:
  * can SEND an announcement, but
  * never joined the multicast group, so it can only RECEIVE unicast.

The laptop must then answer it directly, so the phone discovers the laptop.

Run with:  python3 test_discovery_fallback.py
"""

from __future__ import annotations

import json
import socket
import time

from lanchat import App
from lanchat.discovery import BROADCAST_ADDR, DEFAULT_GROUP, DEFAULT_PORT, MAGIC


def main() -> int:
    # A plain UDP socket bound to the discovery port. It does NOT join the
    # multicast group, so multicast announcements never reach it - only packets
    # addressed to it directly.
    phone = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    phone.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        phone.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    phone.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    phone.settimeout(1.0)

    laptop = App(nick="laptop").start()
    try:
        # Use a private port for the phone so the test does not fight over the
        # discovery port with the laptop's own socket (real devices don't share
        # it). The laptop is told to answer on this port.
        phone.bind(("", 0))
        phone_port = phone.getsockname()[1]

        # Disable the laptop's own announcements entirely, leaving the direct
        # reply as the only way the phone could possibly hear the laptop.
        laptop.discovery._targets = lambda: []  # type: ignore[attr-defined]
        laptop.discovery.port = phone_port

        # The phone announces itself (same as the Android app does).
        hello = {
            "magic": MAGIC,
            "type": "hello",
            "id": "phone01",
            "nick": "phone",
            "tcp_port": 12345,
        }
        phone.sendto(json.dumps(hello).encode("utf-8"), (DEFAULT_GROUP, DEFAULT_PORT))

        # The laptop should discover the phone...
        deadline = time.time() + 10
        while time.time() < deadline and not any(p.nick == "phone" for p in laptop.peers()):
            time.sleep(0.1)
        if not any(p.nick == "phone" for p in laptop.peers()):
            print("FAIL: laptop did not discover the phone")
            return 1
        print("OK: laptop discovered the phone")

        # ...and answer it directly so the phone discovers the laptop.
        got = None
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                data, _addr = phone.recvfrom(4096)
            except socket.timeout:
                continue
            try:
                msg = json.loads(data.decode("utf-8"))
            except ValueError:
                continue
            if msg.get("magic") == MAGIC and msg.get("id") == laptop.peer_id:
                got = msg
                break
        if got is None:
            print("FAIL: laptop never sent a direct reply to the phone")
            return 1
        print("OK: laptop replied directly; phone discovered", got.get("nick"))
        print("\nAll checks passed.")
        return 0
    finally:
        laptop.stop()
        phone.close()


if __name__ == "__main__":
    raise SystemExit(main())

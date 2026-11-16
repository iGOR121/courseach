#!/usr/bin/env python3
"""Self-test: start two nodes on this machine and verify they find each other
and exchange a message.

Run it with:  python3 selftest.py
Exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import sys
import threading
import time

from lanchat import App


def wait_for(predicate, timeout=20.0, interval=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def main() -> int:
    received = []
    lock = threading.Lock()

    def on_message(frame):
        with lock:
            received.append(frame)

    alice = App(nick="alice", on_message=on_message).start()
    bob = App(nick="bob", on_message=on_message).start()

    print(f"alice: id={alice.peer_id} port={alice.port}")
    print(f"bob:   id={bob.peer_id} port={bob.port}")
    print("waiting for discovery...")

    try:
        ok = wait_for(lambda: any(p.nick == "bob" for p in alice.peers()))
        if not ok:
            print("FAIL: alice never discovered bob")
            return 1
        print("OK: discovery works (alice sees bob)")

        peer = alice.resolve("bob")
        assert peer is not None, "resolve failed"
        alice.send(peer, "hello from alice")

        ok = wait_for(lambda: any(f.get("text") == "hello from alice" for f in received))
        if not ok:
            print("FAIL: bob never received the message")
            return 1
        print("OK: messaging works (bob received alice's message)")

        # Messages sent back-to-back should arrive in the same order.
        with lock:
            received.clear()
        texts = [f"ordered {index}" for index in range(10)]
        for text in texts:
            alice.send(peer, text)

        def ordered_arrived():
            with lock:
                return [
                    f.get("text")
                    for f in received
                    if str(f.get("text", "")).startswith("ordered ")
                ]

        ok = wait_for(lambda: len(ordered_arrived()) >= len(texts))
        arrived = ordered_arrived()
        if not ok or arrived != texts:
            print(f"FAIL: messages arrived out of order: {arrived}")
            return 1
        print("OK: back-to-back messages stay in order")

        # Nickname changes should propagate on the next announcement.
        alice.nick = "alice2"
        ok = wait_for(lambda: any(p.nick == "alice2" for p in bob.peers()))
        if not ok:
            print("FAIL: nickname change was not propagated")
            return 1
        print("OK: nickname updates propagate")

        # Leaving should be noticed quickly.
        running_count = len(bob.peers())
        alice.stop()
        ok = wait_for(lambda: len(bob.peers()) < running_count)
        if not ok:
            print("FAIL: bob did not notice alice leaving")
            return 1
        print("OK: peers disappear when they leave")

        print("\nAll checks passed.")
        return 0
    finally:
        alice.stop()
        bob.stop()


if __name__ == "__main__":
    raise SystemExit(main())

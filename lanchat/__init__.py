"""lanchat - a tiny peer-to-peer chat for the local network.

Devices on the same Wi-Fi / LAN find each other automatically (UDP multicast)
and exchange messages directly over TCP. No server, no internet, no
dependencies beyond the Python standard library. A terminal UI and a Tkinter
GUI are included.
"""

from .app import App
from .discovery import Peer

__all__ = ["App", "Peer"]
__version__ = "1.0.0"

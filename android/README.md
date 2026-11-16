# LanChat for Android

A super-simple Android app that speaks the **same protocol** as the Python
`lanchat` app, so Android and desktop devices on the same Wi-Fi can chat with
each other.

It reimplements the protocol natively in Kotlin — the Python code cannot run on
Android directly — but uses the identical UDP multicast discovery and
newline-delimited JSON over TCP:

| Message | Format |
| --- | --- |
| Discovery (UDP multicast `239.255.42.99:47820`) | `{"magic":"lanchat/1","type":"hello","id":..,"nick":..,"tcp_port":..}` |
| Leaving | `{"magic":"lanchat/1","type":"bye","id":..}` |
| TCP frames | `{"type":"hello"/"chat"/"bye", ...}` |

## Files

```
android/
├── settings.gradle.kts
├── build.gradle.kts
├── gradle.properties
├── gradle/wrapper/gradle-wrapper.properties
└── app/
    ├── build.gradle.kts
    ├── proguard-rules.pro
    └── src/main/
        ├── AndroidManifest.xml
        ├── java/com/example/lanchat/
        │   ├── MainActivity.kt   # the single screen (UI only)
        │   ├── ChatService.kt    # foreground service that keeps networking alive
        │   ├── ChatCore.kt       # owns discovery + server, holds the log
        │   ├── Discovery.kt      # UDP multicast announce/listen
        │   ├── ChatServer.kt     # TCP server + one-shot sender
        │   └── Peer.kt
        └── res/
            ├── layout/activity_main.xml
            └── values/{strings,themes}.xml
```

## Building it

The app has been compiled successfully with `assembleDebug` (using Android
Studio's SDK and a JDK 17), so the code builds; it has not been run on a device
from here.

1. Open **Android Studio**, then `File → Open…` and choose the `android/` folder.
2. Let Gradle sync. If it complains that the Gradle wrapper JAR is missing,
   either let Studio create it, or run `gradle wrapper` once in `android/`.
3. Press **Run ▶** with a connected device or emulator.

To produce an installable APK: `Build → Build Bundle(s) / APK(s) → Build APK(s)`,
or from the command line with a JDK 17:

```bash
cd android
JAVA_HOME=/path/to/jdk17 ./gradlew assembleDebug
# apk -> app/build/outputs/apk/debug/app-debug.apk
```

## Using it

- The nickname defaults to your device model; change it and tap **Set**.
- Peers appear in the **To:** spinner automatically (tap **Refresh** to force).
- Type a message and tap **Send**.
- **Add** lets you enter a peer manually as `host:port` (from the other
  device's screen) when a network blocks automatic discovery entirely.
- Tap **Leave** (or the notification's action) to stop.

The status line under the message box shows live discovery diagnostics:

```
You are "Pixel" | listening on port 47821 | id 3f9a1c02
iface=wlan0 lock=true rx=14 tx=9 replied=1 peers=1
```

- `iface` — the network interface discovery chose (should be `wlan0`/`wlan1`).
- `lock` — whether the Wi-Fi multicast lock was acquired (must be `true`).
- `rx` / `tx` — announcements received / sent.
- `peers` — number of peers discovered.

If `peers=0`: `rx=0` means nothing is arriving (network blocks multicast and
broadcast, or wrong interface); `rx>0` means packets arrive but are being
ignored. Use **Add** to connect directly if discovery is blocked.

## How discovery works

Announcements are sent to the multicast group *and* the broadcast address, and
a brand new peer is answered with a **direct unicast** hello. That last part is
what makes it work when only one direction of multicast gets through — a phone
that can send but not receive multicast will still receive the direct reply.


### Why there's a notification

Networking runs in a **foreground service** (`ChatService` + `ChatCore`) so the
phone keeps listening when the app isn't on screen or the screen is off. While
the service runs you'll see a small "LanChat – Listening for peers" notification;
that's what keeps Android from freezing or killing the connection.

### A note on "Connection refused"

If the computer reports `Connection refused` when sending, it means the phone
received the connection but nothing was listening on that port. The usual cause
is the phone having stopped its socket — for example if the app was force-stopped,
the service was left via **Leave**, or (in older builds) it was simply
backgrounded. Keep the service running and the phone stays reachable. The Mac
may still list the phone for a few seconds after it leaves; the next send will
fail once, which is harmless.

## Permissions

Most are install-time permissions; notifications are requested on first launch
(Android 13+):

- `INTERNET`, `ACCESS_NETWORK_STATE`, `ACCESS_WIFI_STATE`
- `CHANGE_WIFI_MULTICAST_STATE` — needed to hold a `MulticastLock`, otherwise
  Android filters out incoming multicast packets and peers never appear.
- `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_DATA_SYNC`, `POST_NOTIFICATIONS` —
  to keep the socket alive in the background.

## Testing interoperability

With the app installed, start the Python side on your computer:

```bash
python3 -m lanchat --nick laptop
```

As long as both are on the same Wi-Fi/LAN, they should see each other within a
few seconds. If they don't, see the troubleshooting notes in the top-level
`README.md` (routers with "AP isolation" block multicast).

### Emulator networking (important)

The Android **emulator** runs behind a virtual NAT, so its address is *not*
reachable from your computer. You will discover it, but sending to it fails
with:

> Could not reach sdk_gphone64_x86_64 (10.111.34.24:35191): [Errno 61] Connection refused

The emulator can reach out (Android → computer works), but the computer cannot
connect back in. Two options:

**1. Use a real phone (recommended).** Put it on the same Wi-Fi as your
computer and everything just works.

**2. Forward a port from your computer into the emulator.** The app now tries a
fixed port, `47821`, so this is predictable. With the device connected via
`adb`:

```bash
adb forward tcp:47821 tcp:47821
```

Then in the Python app add the emulator as a manual peer and talk to it:

```
> /add 127.0.0.1 47821 phone
Now talking to phone (127.0.0.1:47821).
```

`adb forward` must be re-run after each emulator boot. If the app could not get
port 47821 it falls back to a random one; the exact port is shown in the app's
status line ("listening on port ...").

## Limitations

- Messages are **unencrypted** plain text — trusted networks only.
- No offline queue; sending to a peer that just left reports an error.
- Networking lives in a foreground service (visible notification); tapping
  **Leave** stops it.

package com.example.lanchat

import android.content.Context
import android.net.wifi.WifiManager
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.MulticastSocket
import java.net.NetworkInterface
import java.util.Collections
import java.util.concurrent.ConcurrentHashMap

/**
 * Announces this device on the LAN over UDP and tracks the peers it hears from.
 *
 * The wire format is identical to the Python implementation, so Android and
 * desktop devices discover each other:
 *
 *   {"magic":"lanchat/1","type":"hello","id":..,"nick":..,"tcp_port":..}
 *   {"magic":"lanchat/1","type":"bye","id":..}
 *
 * Announcements go to both the multicast group *and* the broadcast address,
 * because some networks pass one but not the other. When a brand new peer is
 * heard we also answer it with a direct unicast hello: a device that cannot
 * receive multicast (common on Wi-Fi) will still receive a direct packet, so
 * this makes discovery work even when it is only one-way.
 *
 * Callbacks fire on background threads; the UI must post them to the main
 * thread itself.
 */
class Discovery(
    context: Context,
    private val peerId: String,
    @Volatile var nick: String,
    @Volatile var tcpPort: Int,
    private val onChanged: () -> Unit,
    private val onStats: () -> Unit = {}
) {
    companion object {
        const val MAGIC = "lanchat/1"
        const val GROUP = "239.255.42.99"
        const val PORT = 47820
        const val BROADCAST_ADDR = "255.255.255.255"
        const val ANNOUNCE_INTERVAL_MS = 3_000L
        const val PEER_TIMEOUT_MS = 12_000L
    }

    private val appContext = context.applicationContext
    private val peers = ConcurrentHashMap<String, Peer>()

    @Volatile private var running = false
    private var socket: MulticastSocket? = null
    private var multicastLock: WifiManager.MulticastLock? = null
    private var receiveThread: Thread? = null
    private var announceThread: Thread? = null
    private var chosenInterface: NetworkInterface? = null

    // Simple counters to show in the UI when peers don't appear.
    @Volatile var sent: Int = 0
        private set
    @Volatile var received: Int = 0
        private set
    @Volatile var replied: Int = 0
        private set
    @Volatile var lockAcquired: Boolean = false
        private set

    fun start() {
        if (running) return
        running = true

        // Without this lock Android filters out incoming multicast packets.
        val wifi = appContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager
        multicastLock = wifi?.createMulticastLock("lanchat")?.apply {
            setReferenceCounted(true)
            acquire()
        }
        lockAcquired = multicastLock?.isHeld == true

        val sock = MulticastSocket(PORT)
        sock.timeToLive = 2
        try {
            sock.broadcast = true
        } catch (_: Exception) {
            // Not fatal: multicast still works.
        }

        val group = InetAddress.getByName(GROUP)
        val iface = pickInterface()
        chosenInterface = iface
        if (iface != null) {
            try {
                sock.networkInterface = iface
            } catch (_: Exception) {
                // Fall back to the default interface below.
            }
            try {
                sock.joinGroup(InetSocketAddress(group, PORT), iface)
            } catch (_: Exception) {
                sock.joinGroup(group)
            }
        } else {
            sock.joinGroup(group)
        }
        socket = sock

        receiveThread = Thread { receiveLoop(sock) }.apply { isDaemon = true; start() }
        announceThread = Thread { announceLoop() }.apply { isDaemon = true; start() }
    }

    fun stop() {
        if (!running) return
        running = false

        try {
            val bye = JSONObject().put("magic", MAGIC).put("type", "bye").put("id", peerId)
            for (address in targets()) {
                try {
                    sendTo(bye, address)
                } catch (_: Exception) {
                    // Best effort.
                }
            }
        } catch (_: Exception) {
            // Best effort.
        }
        try {
            socket?.close()
        } catch (_: Exception) {
            // Best effort.
        }
        try {
            receiveThread?.join(1000)
        } catch (_: Exception) {
            // Best effort.
        }
        try {
            announceThread?.join(1000)
        } catch (_: Exception) {
            // Best effort.
        }
        try {
            multicastLock?.release()
        } catch (_: Exception) {
            // Best effort.
        }

        socket = null
        multicastLock = null
    }

    fun snapshot(): List<Peer> = peers.values.sortedBy { it.nick.lowercase() }

    /** One line describing what discovery is doing; shown in the UI. */
    fun diagnostics(): String {
        val iface = chosenInterface?.name ?: "none"
        return "iface=$iface lock=$lockAcquired rx=$received tx=$sent replied=$replied peers=${peers.size}"
    }

    // -- background loops --------------------------------------------------

    private fun receiveLoop(sock: MulticastSocket) {
        val buffer = ByteArray(4096)
        while (running) {
            val packet = DatagramPacket(buffer, buffer.size)
            try {
                sock.receive(packet)
            } catch (_: Exception) {
                if (!running) break
                continue
            }
            val host = packet.address?.hostAddress ?: continue
            val text = String(packet.data, 0, packet.length, Charsets.UTF_8)
            handle(text, host)
        }
    }

    private fun announceLoop() {
        while (running) {
            try {
                announce()
                prune()
            } catch (_: Exception) {
                if (!running) break
            }
            try {
                onStats()
            } catch (_: Exception) {
                // Ignore UI callback errors.
            }
            try {
                Thread.sleep(ANNOUNCE_INTERVAL_MS)
            } catch (_: InterruptedException) {
                break
            }
        }
    }

    private fun announce() {
        val frame = helloFrame()
        for (address in targets()) {
            try {
                sendTo(frame, address)
                sent++
            } catch (_: Exception) {
                // Broadcast is unavailable on some networks; multicast may
                // still work, so treat this as best-effort.
            }
        }
    }

    private fun helloFrame(): JSONObject = JSONObject()
        .put("magic", MAGIC)
        .put("type", "hello")
        .put("id", peerId)
        .put("nick", nick)
        .put("tcp_port", tcpPort)

    private fun handle(text: String, host: String) {
        val obj = try {
            JSONObject(text)
        } catch (_: Exception) {
            return
        }
        if (obj.optString("magic") != MAGIC) return

        val id = obj.optString("id")
        if (id.isEmpty() || id == peerId) return

        received++

        when (obj.optString("type")) {
            "bye" -> {
                if (peers.remove(id) != null) onChanged()
            }
            "hello" -> {
                val port = obj.optInt("tcp_port", 0)
                if (port <= 0) return
                val name = obj.optString("nick").ifEmpty { "?" }
                val existing = peers[id]
                if (existing == null) {
                    peers[id] = Peer(id, name, host, port)
                    onChanged()
                    // Answer a new peer directly, in case it cannot hear
                    // multicast and only receives unicast.
                    replyTo(host)
                } else {
                    val changed = existing.nick != name ||
                        existing.host != host ||
                        existing.tcpPort != port
                    existing.nick = name
                    existing.host = host
                    existing.tcpPort = port
                    existing.lastSeen = System.currentTimeMillis()
                    if (changed) onChanged()
                }
            }
        }
    }

    private fun replyTo(host: String) {
        try {
            sendTo(helloFrame(), InetAddress.getByName(host))
            replied++
        } catch (_: Exception) {
            // Best effort.
        }
    }

    private fun prune() {
        val cutoff = System.currentTimeMillis() - PEER_TIMEOUT_MS
        var removed = false
        for ((id, peer) in peers) {
            if (peer.lastSeen < cutoff) {
                peers.remove(id)
                removed = true
            }
        }
        if (removed) onChanged()
    }

    // -- sending -----------------------------------------------------------

    private fun targets(): List<InetAddress> {
        val out = mutableListOf<InetAddress>()
        try {
            out.add(InetAddress.getByName(GROUP))
        } catch (_: Exception) {
            // Ignore.
        }
        out.addAll(broadcastAddresses())
        return out.distinct()
    }

    private fun broadcastAddresses(): List<InetAddress> {
        val out = mutableListOf<InetAddress>()
        try {
            out.add(InetAddress.getByName(BROADCAST_ADDR))
        } catch (_: Exception) {
            // Ignore.
        }
        val iface = chosenInterface
        if (iface != null) {
            try {
                for (ia in iface.interfaceAddresses) {
                    ia.broadcast?.let { out.add(it) }
                }
            } catch (_: Exception) {
                // Ignore.
            }
        }
        return out.distinct()
    }

    private fun sendTo(frame: JSONObject, address: InetAddress) {
        val sock = socket ?: return
        val data = frame.toString().toByteArray(Charsets.UTF_8)
        sock.send(DatagramPacket(data, data.size, address, PORT))
    }

    /** Prefer a Wi-Fi interface; fall back to any multicast-capable one. */
    private fun pickInterface(): NetworkInterface? {
        return try {
            val candidates = Collections.list(NetworkInterface.getNetworkInterfaces()).filter { iface ->
                try {
                    iface.isUp && !iface.isLoopback && iface.supportsMulticast()
                } catch (_: Exception) {
                    false
                }
            }
            candidates.firstOrNull { it.name.startsWith("wlan") } ?: candidates.firstOrNull()
        } catch (_: Exception) {
            null
        }
    }
}

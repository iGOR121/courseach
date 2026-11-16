package com.example.lanchat

import android.content.Context
import android.os.Handler
import android.os.Looper
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID

/**
 * Owns the networking so it can outlive the screen.
 *
 * [Discovery] and [ChatServer] used to be started and stopped by the Activity
 * (onStart/onStop), which meant the phone stopped listening as soon as the app
 * was backgrounded or the screen turned off. The Mac still listed it for a
 * while and got "Connection refused" when it connected to the closed port.
 *
 * Now [ChatService] keeps a [ChatCore] running in the foreground, and the
 * Activity merely attaches a [Listener] to render what is happening.
 */
object ChatCore {

    /** Implemented by the Activity to render state; callbacks arrive on the main thread. */
    interface Listener {
        fun onPeersChanged(peers: List<Peer>)
        fun onLog(line: String)
        fun onStatusChanged(status: String)
    }

    private val mainHandler = Handler(Looper.getMainLooper())
    private val dateFormat = SimpleDateFormat("HH:mm:ss", Locale.getDefault())

    val peerId: String = UUID.randomUUID().toString().substring(0, 8)

    @Volatile
    var nick: String = "android"

    @Volatile
    var status: String = "Stopped."
        private set

    @Volatile
    var running: Boolean = false
        private set

    @Volatile
    var listener: Listener? = null

    private var server: ChatServer? = null
    private var discovery: Discovery? = null

    /** Peers added by hand, for when discovery is blocked. */
    private val manualPeers = linkedMapOf<String, Peer>()

    private var baseStatus: String = "Stopped."

    private val log = mutableListOf<String>()

    // -- lifecycle ---------------------------------------------------------

    fun start(context: Context) {
        if (running) return
        try {
            val srv = ChatServer(peerId, nick) { from, text ->
                appendLog("${timestamp()}  $from: $text")
            }
            srv.start()
            server = srv

            val disc = Discovery(
                context.applicationContext,
                peerId,
                nick,
                srv.port,
                onChanged = { notifyPeers() },
                onStats = { pushStatus() }
            )
            disc.start()
            discovery = disc
            running = true

            updateStatus("You are \"$nick\" | listening on port ${srv.port} | id $peerId")
            notifyPeers()
        } catch (e: Exception) {
            running = false
            server = null
            discovery = null
            updateStatus("Could not start networking: ${e.message}")
        }
    }

    fun stop() {
        running = false
        discovery?.stop()
        discovery = null
        server?.stop()
        server = null
        updateStatus("Stopped.")
    }

    // -- actions -----------------------------------------------------------

    fun changeNick(newNick: String) {
        nick = newNick
        server?.localNick = newNick
        discovery?.nick = newNick
        updateStatus("Nickname is now \"$newNick\".")
    }

    fun peers(): List<Peer> {
        val discovered = discovery?.snapshot() ?: emptyList()
        return discovered + manualPeers.values
    }

    fun addManual(host: String, port: Int, nick: String?) {
        if (host.isBlank() || port <= 0) return
        val id = "manual:$host:$port"
        val peer = Peer(id, nick ?: host, host, port)
        manualPeers[id] = peer
        appendLog("${timestamp()}  Added ${peer.label}")
        notifyPeers()
    }

    fun send(peer: Peer, text: String) {
        if (server == null) {
            appendLog("${timestamp()}  Not connected.")
            return
        }
        appendLog("${timestamp()}  me -> ${peer.nick}: $text")
        Thread {
            try {
                ChatServer.send(peer.host, peer.tcpPort, peerId, nick, text)
            } catch (e: Exception) {
                appendLog("${timestamp()}  Could not reach ${peer.nick}: ${e.message}")
            }
        }.apply { isDaemon = true; start() }
    }

    // -- log ---------------------------------------------------------------

    fun appendLog(line: String) {
        synchronized(log) {
            log.add(line)
            if (log.size > 500) log.removeAt(0)
        }
        mainHandler.post { listener?.onLog(line) }
    }

    fun logSnapshot(): List<String> = synchronized(log) { log.toList() }

    // -- notifications -----------------------------------------------------

    private fun notifyPeers() {
        mainHandler.post { listener?.onPeersChanged(peers()) }
    }

    private fun updateStatus(text: String) {
        baseStatus = text
        pushStatus()
    }

    /** Re-render the status line, including live discovery diagnostics. */
    fun pushStatus() {
        val diag = discovery?.diagnostics()
        val full = if (diag != null) "$baseStatus\n$diag" else baseStatus
        status = full
        mainHandler.post { listener?.onStatusChanged(full) }
    }

    private fun timestamp(): String = dateFormat.format(Date())
}

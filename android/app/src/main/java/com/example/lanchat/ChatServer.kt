package com.example.lanchat

import org.json.JSONObject
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket

/**
 * Receives and sends chat messages over TCP using the same newline-delimited
 * JSON frames as the Python implementation:
 *
 *   {"type":"hello","id":..,"nick":..}
 *   {"type":"chat","id":..,"nick":..,"text":..,"ts":..}
 *   {"type":"bye"}
 *
 * Each delivery is a short-lived connection that is written and closed.
 */
class ChatServer(
    private val localId: String,
    @Volatile var localNick: String,
    private val onMessage: (fromNick: String, text: String) -> Unit
) {
    @Volatile private var running = false
    private var serverSocket: ServerSocket? = null
    private var acceptThread: Thread? = null

    @Volatile
    var port: Int = 0
        private set

    fun start() {
        if (running) return
        // Prefer a fixed port so tools like `adb forward` stay predictable
        // across restarts. Fall back to an ephemeral port if it is taken.
        val socket = ServerSocket()
        try {
            socket.reuseAddress = true
        } catch (_: Exception) {
            // Best effort.
        }
        try {
            socket.bind(InetSocketAddress(PREFERRED_PORT))
        } catch (_: Exception) {
            socket.bind(InetSocketAddress(0))
        }
        serverSocket = socket
        port = socket.localPort
        running = true

        acceptThread = Thread {
            while (running) {
                val client = try {
                    socket.accept()
                } catch (_: Exception) {
                    if (!running) break else continue
                }
                Thread { handleClient(client) }.apply { isDaemon = true; start() }
            }
        }.apply {
            isDaemon = true
            start()
        }
    }

    fun stop() {
        running = false
        try {
            serverSocket?.close()
        } catch (_: Exception) {
            // Best effort.
        }
        try {
            acceptThread?.join(1000)
        } catch (_: Exception) {
            // Best effort.
        }
        serverSocket = null
    }

    private fun handleClient(client: Socket) {
        try {
            client.use { sock ->
                try {
                    sock.soTimeout = 30_000
                } catch (_: Exception) {
                    // Best effort.
                }
                val reader = sock.getInputStream().bufferedReader(Charsets.UTF_8)
                while (true) {
                    val line = reader.readLine() ?: break
                    val trimmed = line.trim()
                    if (trimmed.isEmpty()) continue
                    val obj = try {
                        JSONObject(trimmed)
                    } catch (_: Exception) {
                        continue
                    }
                    if (obj.optString("type") == "chat" && obj.optString("id") != localId) {
                        val from = obj.optString("nick").ifEmpty { "?" }
                        onMessage(from, obj.optString("text"))
                    }
                }
            }
        } catch (_: Exception) {
            // A bad connection must never take down the listener.
        }
    }

    companion object {
        /** Fixed TCP port, so port forwarding (e.g. adb) is predictable. */
        const val PREFERRED_PORT = 47821

        /**
         * Deliver a single message to a peer. Blocks and may throw, so call it
         * from a background thread.
         */
        fun send(host: String, peerPort: Int, localId: String, localNick: String, text: String) {
            Socket().use { socket ->
                socket.connect(InetSocketAddress(host, peerPort), 5_000)
                val writer = socket.getOutputStream().bufferedWriter(Charsets.UTF_8)

                writer.write(
                    JSONObject()
                        .put("type", "hello")
                        .put("id", localId)
                        .put("nick", localNick)
                        .toString()
                )
                writer.newLine()

                writer.write(
                    JSONObject()
                        .put("type", "chat")
                        .put("id", localId)
                        .put("nick", localNick)
                        .put("text", text)
                        .put("ts", System.currentTimeMillis() / 1000.0)
                        .toString()
                )
                writer.newLine()

                writer.write(JSONObject().put("type", "bye").toString())
                writer.newLine()
                writer.flush()
            }
        }
    }
}

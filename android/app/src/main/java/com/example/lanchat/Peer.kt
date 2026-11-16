package com.example.lanchat

/** A chat partner discovered on the local network. */
data class Peer(
    val id: String,
    var nick: String,
    var host: String,
    var tcpPort: Int,
    var lastSeen: Long = System.currentTimeMillis()
) {
    val address: String get() = "$host:$tcpPort"
    val label: String get() = "$nick ($address)"
}

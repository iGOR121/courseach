package com.example.lanchat

import android.Manifest
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

/**
 * The single screen of the app: nickname, a peer picker, the conversation and a
 * message box.
 *
 * Networking is owned by [ChatService] / [ChatCore] so it survives the screen
 * turning off. This Activity just attaches a listener while it is visible.
 */
class MainActivity : AppCompatActivity(), ChatCore.Listener {

    private lateinit var nickInput: EditText
    private lateinit var peerSpinner: Spinner
    private lateinit var messages: LinearLayout
    private lateinit var scroll: ScrollView
    private lateinit var messageInput: EditText
    private lateinit var status: TextView

    /** Backs the spinner; kept in the same order as the spinner labels. */
    private val peerList = mutableListOf<Peer>()
    private lateinit var peerAdapter: ArrayAdapter<String>

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        nickInput = findViewById(R.id.nickInput)
        peerSpinner = findViewById(R.id.peerSpinner)
        messages = findViewById(R.id.messages)
        scroll = findViewById(R.id.scroll)
        messageInput = findViewById(R.id.messageInput)
        status = findViewById(R.id.status)

        peerAdapter = ArrayAdapter(
            this,
            android.R.layout.simple_spinner_item,
            mutableListOf<String>()
        )
        peerAdapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item)
        peerSpinner.adapter = peerAdapter

        ChatCore.nick = defaultNick()
        nickInput.setText(ChatCore.nick)

        findViewById<Button>(R.id.nickButton).setOnClickListener { applyNick() }
        findViewById<Button>(R.id.refreshButton).setOnClickListener {
            onPeersChanged(ChatCore.peers())
            ChatCore.pushStatus()
        }
        findViewById<Button>(R.id.sendButton).setOnClickListener { sendCurrent() }
        findViewById<Button>(R.id.addButton).setOnClickListener { addPeerDialog() }
        findViewById<Button>(R.id.leaveButton).setOnClickListener { leave() }
        messageInput.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_SEND) {
                sendCurrent()
                true
            } else {
                false
            }
        }

        requestNotificationPermission()
        startChatService()
    }

    override fun onStart() {
        super.onStart()
        ChatCore.listener = this

        // Re-render the conversation that accumulated while we were away.
        messages.removeAllViews()
        ChatCore.logSnapshot().forEach { appendLine(it) }
        onPeersChanged(ChatCore.peers())
        status.text = ChatCore.status
    }

    override fun onStop() {
        super.onStop()
        if (ChatCore.listener === this) {
            ChatCore.listener = null
        }
    }

    // -- ChatCore.Listener -------------------------------------------------

    override fun onPeersChanged(peers: List<Peer>) {
        peerList.clear()
        peerList.addAll(peers)
        peerAdapter.clear()
        peerAdapter.addAll(peers.map { it.label })
        peerAdapter.notifyDataSetChanged()
    }

    override fun onLog(line: String) {
        appendLine(line)
    }

    override fun onStatusChanged(status: String) {
        this.status.text = status
    }

    // -- actions -----------------------------------------------------------

    private fun applyNick() {
        ChatCore.changeNick(currentNick())
    }

    private fun sendCurrent() {
        val text = messageInput.text.toString().trim()
        if (text.isEmpty()) return

        val peer = peerList.getOrNull(peerSpinner.selectedItemPosition)
        if (peer == null) {
            appendLine("No peer selected. Wait for someone to appear, then pick them.")
            ChatCore.pushStatus()
            return
        }

        messageInput.setText("")
        ChatCore.send(peer, text)
    }

    private fun leave() {
        stopService(Intent(this, ChatService::class.java))
        Toast.makeText(this, "Left the chat.", Toast.LENGTH_SHORT).show()
    }

    /**
     * Add a peer by hand. The other device's address and port are shown in its
     * startup banner (Python) or status line (Android). Useful when a network
     * blocks the automatic discovery entirely.
     */
    private fun addPeerDialog() {
        val input = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_TEXT
            hint = "192.168.1.10:47821"
        }
        AlertDialog.Builder(this)
            .setTitle("Add peer by address")
            .setMessage("Enter host:port from the other device's screen.")
            .setView(input)
            .setPositiveButton("Add") { _, _ ->
                val text = input.text.toString().trim()
                val parts = text.split(":")
                val port = parts.getOrNull(1)?.toIntOrNull()
                if (parts.size == 2 && port != null) {
                    ChatCore.addManual(parts[0], port, null)
                    onPeersChanged(ChatCore.peers())
                } else {
                    appendLine("Use the form host:port, for example 192.168.1.10:47821")
                }
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    // -- helpers -----------------------------------------------------------

    private fun startChatService() {
        val intent = Intent(this, ChatService::class.java).setAction(ChatService.ACTION_START)
        ContextCompat.startForegroundService(this, intent)
    }

    private fun requestNotificationPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        val granted = ContextCompat.checkSelfPermission(
            this,
            Manifest.permission.POST_NOTIFICATIONS
        ) == PackageManager.PERMISSION_GRANTED
        if (!granted) {
            ActivityCompat.requestPermissions(
                this,
                arrayOf(Manifest.permission.POST_NOTIFICATIONS),
                1001
            )
        }
    }

    private fun currentNick(): String =
        nickInput.text.toString().trim().ifEmpty { "android" }

    private fun defaultNick(): String =
        (Build.MODEL ?: "android")
            .replace(Regex("[^A-Za-z0-9_-]"), "")
            .ifEmpty { "android" }
            .take(20)

    private fun appendLine(text: String) {
        val view = TextView(this).apply {
            this.text = text
            setPadding(8, 6, 8, 6)
        }
        messages.addView(view)
        scroll.post { scroll.fullScroll(View.FOCUS_DOWN) }
    }
}

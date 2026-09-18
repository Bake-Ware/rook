package systems.bake.rook

import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.util.Base64
import android.view.inputmethod.EditorInfo
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.FileProvider
import systems.bake.rook.databinding.ActivityMainBinding
import java.io.ByteArrayOutputStream
import java.io.File

/**
 * Home screen = the conversation with Sojourn, by voice or text.
 *
 *  - Talk / Interrupt / Sleep drive the voice session.
 *  - The text box sends a typed turn (reply comes back as text, not speech).
 *  - The camera button captures a photo and sends it to the vision model.
 *
 * Band settings, permission grants and the voice endpoint live in SettingsActivity.
 */
class MainActivity : AppCompatActivity(), VoiceBus.Listener {

    private lateinit var b: ActivityMainBinding
    private val prefs by lazy { getSharedPreferences("rook", MODE_PRIVATE) }
    private var curBot: ChatMessage? = null
    private val assistantTurns = mutableMapOf<Int, ChatMessage>()
    private lateinit var chat: ChatAdapter
    private val pendingUsers = ArrayDeque<ChatMessage>()
    private var eventTurn: Int? = null
    private val userTurns = mutableSetOf<TurnKey>()
    private lateinit var panels: ConversationPanels
    private var connectionGeneration = -1L
    private var state = "idle"
    private var photoUri: Uri? = null

    private val micPermLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { ok ->
            if (ok) voiceOn(openSession = true) else addSystem("microphone permission denied")
        }

    private val cameraLauncher =
        registerForActivityResult(ActivityResultContracts.TakePicture()) { ok ->
            val uri = photoUri
            if (ok && uri != null) sendPhoto(uri) else addSystem("photo cancelled")
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityMainBinding.inflate(layoutInflater)
        setContentView(b.root)
        chat = ChatAdapter { prefs.getBoolean("show_thinking", false) }
        b.chatList.layoutManager = androidx.recyclerview.widget.LinearLayoutManager(this).apply { stackFromEnd = true }
        b.chatList.adapter = chat
        panels = ConversationPanels(this, b) { key ->
            val index = chat.expand(key)
            if (index >= 0) b.chatList.post { b.chatList.smoothScrollToPosition(index) }
            else android.widget.Toast.makeText(this, "No chat message available for this turn", android.widget.Toast.LENGTH_SHORT).show()
        }
        b.versionLabel.text = "APK ${BuildConfig.VERSION_NAME} · ${BuildConfig.VERSION_CODE}"
        maybeRequestNotifications()

        b.btnSettings.setOnClickListener { startActivity(Intent(this, SettingsActivity::class.java)) }
        b.btnTalk.setOnClickListener {
            when (state) {
                "thinking", "speaking" -> VoiceService.interrupt(this)
                "idle" -> { panels.listening(); voiceOn(openSession = true) }
                else -> { panels.listening(); VoiceService.start(this, url(), insecure()) }   // standby/listening -> talk now
            }
        }
        b.btnSleep.setOnClickListener {
            if (state == "idle") voiceOn() else { VoiceService.endSession(this); addSystem("sleeping — say \"hey sojourn\"") }
        }
        b.btnSend.setOnClickListener { sendTyped() }
        b.input.setOnEditorActionListener { _, id, _ ->
            if (id == EditorInfo.IME_ACTION_SEND) { sendTyped(); true } else false
        }
        b.btnCamera.setOnClickListener { takePhoto() }

        if (hasMic() && prefs.getBoolean("wake_enabled", true) && VoiceBus.state == "idle") voiceOn()
        if (prefs.getBoolean("autostart", false)) {
            WorkerService.start(this,
                prefs.getString("hub", BuildConfig.DEFAULT_HUB) ?: BuildConfig.DEFAULT_HUB,
                prefs.getString("psk", BuildConfig.DEFAULT_PSK) ?: BuildConfig.DEFAULT_PSK,
                prefs.getString("name", Build.MODEL ?: "android") ?: "android")
        }
    }

    // ---- input ----------------------------------------------------------

    private fun sendTyped() {
        val t = b.input.text.toString().trim()
        if (t.isEmpty()) return
        b.input.setText("")
        curBot = null
        pendingUsers.addLast(addBubble(t, user = true))
        panels.begin("Planning")
        VoiceService.send(this, t, null, speak = false)
    }

    private fun takePhoto() {
        val dir = File(cacheDir, "captures").apply { mkdirs() }
        val f = File(dir, "shot.jpg")
        photoUri = FileProvider.getUriForFile(this, "$packageName.fileprovider", f)
        try {
            cameraLauncher.launch(photoUri)
        } catch (t: Throwable) {
            addSystem("no camera app available")
        }
    }

    /** Downscale + JPEG-compress so a phone photo doesn't become a multi-MB base64 blob. */
    private fun sendPhoto(uri: Uri) {
        val caption = b.input.text.toString().trim()
        b.input.setText("")
        val bmp = contentResolver.openInputStream(uri).use { BitmapFactory.decodeStream(it) }
        if (bmp == null) { addSystem("couldn't read photo"); return }
        val max = 1024
        val scale = minOf(1f, max.toFloat() / maxOf(bmp.width, bmp.height))
        val small = if (scale < 1f)
            Bitmap.createScaledBitmap(bmp, (bmp.width * scale).toInt(), (bmp.height * scale).toInt(), true)
        else bmp
        val out = ByteArrayOutputStream()
        small.compress(Bitmap.CompressFormat.JPEG, 80, out)
        val b64 = Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP)
        curBot = null
        pendingUsers.addLast(chat.add(caption, user = true, image = small))
        scrollToEnd()
        addSystem("photo sent (${out.size() / 1024} KB)")
        panels.begin("Planning")
        VoiceService.send(this, caption.ifEmpty { "What is in this image?" }, b64, speak = false)
    }

    private fun url() = prefs.getString("voice_url", BuildConfig.DEFAULT_VOICE_URL) ?: BuildConfig.DEFAULT_VOICE_URL
    private fun insecure() = prefs.getBoolean("voice_insecure", false)
    private fun hasMic() = checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

    private fun voiceOn(openSession: Boolean = false) {
        if (!hasMic()) { micPermLauncher.launch(android.Manifest.permission.RECORD_AUDIO); return }
        if (openSession) VoiceService.start(this, url(), insecure()) else VoiceService.standby(this, url(), insecure())
    }

    // ---- VoiceBus.Listener (main thread) --------------------------------

    override fun onState(s: String) {
        syncConnection()
        if (s == "thinking") eventTurn?.let { id ->
            val key = TurnKey(connectionGeneration, id)
            if (key !in userTurns && pendingUsers.isNotEmpty()) {
                userTurns.add(key); chat.bindTurn(pendingUsers.removeFirst(), key)
            }
        }
        panels.state(s)
        state = s
        b.stateText.text = getString(when (s) {
            "standby" -> R.string.st_standby
            "listening" -> R.string.st_listening
            "thinking" -> R.string.st_thinking
            "speaking" -> R.string.st_speaking
            else -> R.string.st_idle
        })
        b.btnTalk.contentDescription = getString(if (s == "thinking" || s == "speaking") R.string.voice_interrupt else R.string.btn_talk)
        b.btnSleep.contentDescription = getString(if (s == "idle") R.string.voice_on else R.string.voice_sleep)
        b.btnTalk.setImageResource(if (s in listOf("listening", "thinking", "speaking")) R.drawable.ic_mic_active else R.drawable.ic_mic)
    }

    override fun onTranscript(text: String) { curBot = null; addBubble(text, user = true) }
    override fun onAssistantDelta(text: String) {
        val tv = curBot ?: addBubble("", user = false).also { curBot = it }
        if (tv.text.isNotEmpty()) tv.text += " "
        tv.text += text; chat.changed(tv); scrollToEnd()
    }
    private fun syncConnection() {
        if (connectionGeneration != VoiceBus.connectionGeneration) {
            connectionGeneration = VoiceBus.connectionGeneration
            panels.sync(connectionGeneration); assistantTurns.clear(); curBot = null; eventTurn = null
        }
    }

    override fun onTranscript(text: String, turn: Int?) {
        syncConnection()
        if (turn == null) { onTranscript(text); return }
        curBot = null
        panels.turn(turn)
        val key = TurnKey(connectionGeneration, turn)
        userTurns.add(key)
        chat.bindTurn(addBubble(text, user = true), key)
    }

    override fun onAssistantDelta(text: String, turn: Int?) {
        syncConnection()
        if (turn == null) { onAssistantDelta(text); return }
        val tv = assistantTurns.getOrPut(turn) { addBubble("", user = false).also { chat.bindTurn(it, TurnKey(connectionGeneration, turn)) } }
        curBot = tv
        if (tv.text.isNotEmpty()) tv.text += " "
        tv.text += text
        chat.changed(tv)
        panels.turn(turn)
        scrollToEnd()
    }

    override fun onTurn(turn: Int) { syncConnection(); eventTurn = turn; panels.turn(turn) }
    override fun onActivity(event: ActivityEvent) { syncConnection(); panels.activity(event) }
    override fun onDecision(decision: Decision) {
        syncConnection()
        if (prefs.getBoolean("show_thinking", false)) {
            chat.decision(TurnKey(connectionGeneration, decision.turn), decision)
            panels.decision(decision)
        }
    }

    override fun onAssistantDone() { curBot = null; panels.done() }
    override fun onInterrupt() { curBot?.let { it.alpha = 0.5f; chat.changed(it) }; curBot = null; panels.interrupt() }
    override fun onError(msg: String) { syncConnection(); panels.note(msg, failed = true); panels.done() }
    override fun onWake() { addSystem("wake word"); panels.listening() }
    override fun onBye(mode: String) { addSystem(if (mode == "off") "voice off" else "sleeping — say \"hey sojourn\"") }
    override fun onTool(title: String, status: String) { syncConnection(); panels.tool(title, status) }

    // ---- chat rendering -------------------------------------------------

    private fun addBubble(text: String, user: Boolean): ChatMessage = chat.add(text, user).also { scrollToEnd() }

    private fun addSystem(text: String) { syncConnection(); panels.note(text) }

    private fun scrollToEnd() { b.chatList.post { if (chat.itemCount > 0) b.chatList.scrollToPosition(chat.itemCount - 1) } }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
    }

    override fun onResume() {
        super.onResume()
        chat.notifyDataSetChanged()
        panels.resume()
        VoiceBus.listener = this
        onState(VoiceBus.state)
        if (intent?.action == Intent.ACTION_ASSIST) {
            intent.action = null
            voiceOn(openSession = true)
        }
    }

    override fun onPause() { panels.pause(); VoiceBus.listener = null; super.onPause() }

    private val notifPermLauncher = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }
    private fun maybeRequestNotifications() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
            notifPermLauncher.launch(android.Manifest.permission.POST_NOTIFICATIONS)
    }
}

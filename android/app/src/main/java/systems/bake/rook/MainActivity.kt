package systems.bake.rook

import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.util.Base64
import android.view.Gravity
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
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
    private var curBot: TextView? = null
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
        maybeRequestNotifications()

        b.btnSettings.setOnClickListener { startActivity(Intent(this, SettingsActivity::class.java)) }
        b.btnTalk.setOnClickListener {
            when (state) {
                "thinking", "speaking" -> VoiceService.interrupt(this)
                "idle" -> voiceOn(openSession = true)
                else -> VoiceService.start(this, url(), insecure())   // standby/listening -> talk now
            }
        }
        b.btnInterrupt.setOnClickListener { VoiceService.interrupt(this) }
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
        addBubble(t, user = true)
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
        addImageBubble(small, caption)
        addSystem("photo sent (${out.size() / 1024} KB)")
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
        state = s
        b.stateText.text = getString(when (s) {
            "standby" -> R.string.st_standby
            "listening" -> R.string.st_listening
            "thinking" -> R.string.st_thinking
            "speaking" -> R.string.st_speaking
            else -> R.string.st_idle
        })
        b.btnTalk.text = getString(if (s == "thinking" || s == "speaking") R.string.voice_interrupt else R.string.btn_talk)
        b.btnSleep.text = getString(if (s == "idle") R.string.voice_on else R.string.voice_sleep)
        b.btnInterrupt.isEnabled = s == "thinking" || s == "speaking"
    }

    override fun onTranscript(text: String) { curBot = null; addBubble(text, user = true) }
    override fun onAssistantDelta(text: String) {
        val tv = curBot ?: addBubble("", user = false).also { curBot = it }
        if (tv.text.isNotEmpty()) tv.append(" ")
        tv.append(text); scrollToEnd()
    }
    override fun onAssistantDone() { curBot = null }
    override fun onInterrupt() { curBot?.alpha = 0.5f; curBot = null }
    override fun onError(msg: String) { addSystem("error: $msg") }
    override fun onWake() { addSystem("wake word") }
    override fun onBye(mode: String) { addSystem(if (mode == "off") "voice off" else "sleeping — say \"hey sojourn\"") }
    override fun onTool(title: String, status: String) { addSystem("$title · $status") }

    // ---- chat rendering -------------------------------------------------

    private fun addBubble(text: String, user: Boolean): TextView {
        val tv = TextView(this).apply {
            this.text = text
            textSize = 16f
            setTextColor(Color.WHITE)
            setPadding(dp(14), dp(10), dp(14), dp(10))
            setBackgroundResource(if (user) R.drawable.bubble_user else R.drawable.bubble_bot)
            maxWidth = (resources.displayMetrics.widthPixels * 0.8).toInt()
        }
        b.chat.addView(tv, rowParams(user)); scrollToEnd(); return tv
    }

    private fun addImageBubble(bmp: Bitmap, caption: String) {
        val iv = ImageView(this).apply {
            setImageBitmap(bmp)
            adjustViewBounds = true
            maxWidth = (resources.displayMetrics.widthPixels * 0.55).toInt()
        }
        b.chat.addView(iv, rowParams(true))
        if (caption.isNotEmpty()) addBubble(caption, user = true)
        scrollToEnd()
    }

    private fun rowParams(user: Boolean) =
        LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT)
            .apply { topMargin = dp(6); gravity = if (user) Gravity.END else Gravity.START }

    private fun addSystem(text: String) {
        val tv = TextView(this).apply {
            this.text = text; textSize = 12f; alpha = 0.6f; setPadding(dp(6), dp(4), dp(6), dp(4))
        }
        b.chat.addView(tv, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT)
            .apply { gravity = Gravity.CENTER_HORIZONTAL })
        scrollToEnd()
    }

    private fun scrollToEnd() { b.chatScroll.post { b.chatScroll.fullScroll(View.FOCUS_DOWN) } }
    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    override fun onResume() {
        super.onResume()
        VoiceBus.listener = this
        onState(VoiceBus.state)
    }

    override fun onPause() { VoiceBus.listener = null; super.onPause() }

    private val notifPermLauncher = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }
    private fun maybeRequestNotifications() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
            notifPermLauncher.launch(android.Manifest.permission.POST_NOTIFICATIONS)
    }
}

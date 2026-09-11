package systems.bake.rook

import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.ToneGenerator
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.NoiseSuppressor
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import kotlin.concurrent.thread

/**
 * Foreground service (microphone type) that owns THE mic and runs two things on it:
 *
 *  - always: the on-device wake-word detector ("hey sojourn", openWakeWord ONNX)
 *  - on demand: a VoiceClient session to the kaiju voice-agent, fed from the same
 *    AudioRecord. Sessions open on wake word (or a manual Start) and close after
 *    IDLE_CLOSE_MS of the server sitting in "listening" with nothing said.
 *
 * Normal speech interrupts during playback when platform AEC is enabled.
 *
 * UI observes via [VoiceBus] (main-thread callbacks) — no binder needed.
 */
class VoiceService : Service() {

    @Volatile private var client: VoiceClient? = null
    private var generation = 0L
    private var sessionWanted = false
    private var retries = 0
    @Volatile private var aecAvailable = false
    private var detector: WakeWordDetector? = null
    private var micThread: Thread? = null
    @Volatile private var micRunning = false
    @Volatile private var standby = false
    private var wakeLock: PowerManager.WakeLock? = null
    private val main = Handler(Looper.getMainLooper())
    @Volatile private var lastServerState = "idle"
    @Volatile private var lastActivityAt = 0L
    @Volatile private var lastWakeAt = 0L
    private lateinit var url: String
    private var insecure = false
    private var token = ""
    private var wakeEnabled = true

    override fun onCreate() {
        super.onCreate()
        inst = this
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "Rook voice", NotificationManager.IMPORTANCE_LOW))
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val prefs = getSharedPreferences("rook", MODE_PRIVATE)
        url = intent?.getStringExtra(EXTRA_URL) ?: prefs.getString("voice_url", "") ?: ""
        insecure = intent?.getBooleanExtra(EXTRA_INSECURE, false) ?: prefs.getBoolean("voice_insecure", false)
        token = prefs.getString("voice_token", "") ?: ""
        wakeEnabled = prefs.getBoolean("wake_enabled", true)

        when (intent?.action) {
            ACTION_INTERRUPT -> { client?.interrupt(); return START_STICKY }
            ACTION_END_SESSION -> { closeSession(); return START_STICKY }
            ACTION_STOP -> { standby = false; closeSession(); stopMic(); stopForegroundCompat(); stopSelf(); return START_NOT_STICKY }
            ACTION_SESSION -> {            // manual push-to-talk: open a session now
                standby = true; ensureForeground(); ensureMic(); openSession(); return START_STICKY
            }
        }
        // default / ACTION_STANDBY: mic on, wake word armed, no session yet
        if (url.isEmpty()) { stopSelf(); return START_NOT_STICKY }
        standby = true
        ensureForeground()
        val p = pending
        if (p != null) {
            pending = null
            submit(p.first, p.second, p.third)
        } else ensureMic()
        setState(if (wakeEnabled) "standby" else "idle")
        return START_STICKY
    }

    // ---- session --------------------------------------------------------

    /** Text/image chat: opens a session if needed, but never turns the mic on by itself. */
    fun submit(text: String, imageB64: String? = null, speak: Boolean = false) {
        if (url.isEmpty()) {
            url = getSharedPreferences("rook", MODE_PRIVATE).getString("voice_url", "") ?: ""
            if (url.isEmpty()) { VoiceBus.listener?.onError("no voice server configured"); return }
        }
        ensureForeground()
        openSession()
        val c = client ?: return
        if (imageB64 != null) c.sendImage(imageB64, text, speak) else c.sendText(text, speak)
        lastActivityAt = SystemClock.elapsedRealtime()
    }

    private fun openSession() {
        if (client?.isRunning == true) return
        sessionWanted = true
        val mine = ++generation
        fun current(action: () -> Unit) = post { if (mine == generation) action() }
        lastActivityAt = SystemClock.elapsedRealtime()
        client = VoiceClient(this, url, insecure, object : VoiceClient.Listener {
            override fun onState(state: String) = current {
                if (state == "listening") retries = 0
                lastServerState = state; lastActivityAt = SystemClock.elapsedRealtime()
                setState(state)
            }
            override fun onTranscript(text: String) = current { lastActivityAt = SystemClock.elapsedRealtime(); VoiceBus.listener?.onTranscript(text) }
            override fun onAssistantDelta(text: String) = current { VoiceBus.listener?.onAssistantDelta(text) }
            override fun onAssistantDone() = current { VoiceBus.listener?.onAssistantDone() }
            override fun onInterrupt() = current { VoiceBus.listener?.onInterrupt() }
            override fun onError(msg: String) = current { VoiceBus.listener?.onError(msg) }
            override fun onTool(title: String, status: String) = current { VoiceBus.listener?.onTool(title, status) }
            override fun onBye(mode: String, afterMs: Long) = current {
                Log.i(TAG, "bye mode=$mode after=${afterMs}ms")
                VoiceBus.listener?.onBye(mode)
                main.postDelayed({
                    if (mine != generation) return@postDelayed
                    if (mode == "off") { standby = false; closeSession(); stopMic(); stopForegroundCompat(); stopSelf() }
                    else closeSession()
                }, afterMs.coerceIn(0L, 15_000L))
            }
            override fun onClosed() = current {
                client = null
                main.removeCallbacks(idleCheck)
                if (sessionWanted && retries < 5) {
                    val delay = minOf(15_000L, 1000L shl retries++)
                    setState("reconnecting")
                    main.postDelayed({ if (mine == generation && sessionWanted) openSession() }, delay)
                    return@current
                }
                sessionWanted = false
                if (standby) setState(if (wakeEnabled) "standby" else "idle")
                else { stopMic(); stopForegroundCompat(); stopSelf() }
            }
        }, ownMic = false, token = token).also { it.setAecAvailable(aecAvailable); it.connect() }
        main.removeCallbacks(idleCheck)
        main.postDelayed(idleCheck, IDLE_CLOSE_MS)
    }

    private fun closeSession() {
        sessionWanted = false; ++generation; retries = 0
        main.removeCallbacks(idleCheck)
        val old = client; client = null; old?.close()
        if (standby) setState(if (wakeEnabled) "standby" else "idle")
    }

    private val idleCheck = object : Runnable {
        override fun run() {
            val c = client ?: return
            val idle = SystemClock.elapsedRealtime() - lastActivityAt
            if (lastServerState == "listening" && idle >= IDLE_CLOSE_MS) { closeSession() }
            else main.postDelayed(this, 2000)
        }
    }

    // ---- mic (shared by detector + session) -------------------------------

    @SuppressLint("MissingPermission")
    private fun ensureMic() {
        if (micRunning) return
        if (micThread?.isAlive == true) {
            main.postDelayed({ if (inst === this && (standby || sessionWanted)) ensureMic() }, 200)
            return
        }
        micRunning = true
        micThread = thread(name = "voice-mic") {
            var rec: AudioRecord? = null
            var echo: AcousticEchoCanceler? = null
            var noise: NoiseSuppressor? = null
            var speech: SpeechDetector? = null
            var wake: WakeWordDetector? = null
            val audio = getSystemService(Context.AUDIO_SERVICE) as AudioManager
            val oldMode = audio.mode
            val oldSpeaker = audio.isSpeakerphoneOn
            try {
                speech = SpeechDetector(this)
                if (wakeEnabled) wake = WakeWordDetector(this, WAKE_MODEL, WAKE_THRESHOLD)
                detector = wake
                val minBuf = AudioRecord.getMinBufferSize(VoiceClient.SR_IN, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
                audio.mode = AudioManager.MODE_IN_COMMUNICATION
                if (!audio.isBluetoothScoOn && !audio.isWiredHeadsetOn) audio.isSpeakerphoneOn = true
                rec = AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, VoiceClient.SR_IN,
                    AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuf, VoiceClient.FRAME_BYTES * 8))
                check(rec.state == AudioRecord.STATE_INITIALIZED) { "mic init failed" }
                if (AcousticEchoCanceler.isAvailable()) echo = AcousticEchoCanceler.create(rec.audioSessionId)?.also { it.enabled = true }
                if (NoiseSuppressor.isAvailable()) noise = NoiseSuppressor.create(rec.audioSessionId)?.also { it.enabled = true }
                aecAvailable = echo?.enabled == true
                client?.setAecAvailable(aecAvailable)
                rec.startRecording()
                val buf = ByteArray(VoiceClient.FRAME_BYTES)
                var wasActive = false
                while (micRunning) {
                    var off = 0
                    while (off < buf.size && micRunning) {
                        val n = rec.read(buf, off, buf.size - off)
                        check(n > 0) { "microphone read failed" }
                        off += n
                    }
                    if (off != buf.size) continue
                    val sp = speech.feed(buf)
                    val c = client
                    val active = c?.isRunning == true
                    c?.pushFrame(buf, speech = sp)
                    // Wake recognition has no role inside an active conversation.
                    // Never run it against the assistant's own speech.
                    if (active != wasActive) wake?.reset()
                    wasActive = active
                    if (!active && wake != null && wake.feed(buf, speech = speech.recentSpeech)) onWake(wake)
                }
            } catch (error: Exception) {
                post { VoiceBus.listener?.onError("Voice microphone: ${error.message}") }
            } finally {
                micRunning = false; aecAvailable = false
                client?.setAecAvailable(false)
                try { rec?.stop() } catch (_: Exception) {}
                echo?.release(); noise?.release(); rec?.release()
                speech?.close(); wake?.close(); detector = null
                audio.isSpeakerphoneOn = oldSpeaker; audio.mode = oldMode
            }
        }
    }

    private fun onWake(d: WakeWordDetector) {
        val now = SystemClock.elapsedRealtime()
        if (now - lastWakeAt < WAKE_REFRACTORY_MS) return
        lastWakeAt = now
        Log.i(TAG, "WAKE score=${d.lastScore}")
        d.reset()
        try { ToneGenerator(AudioManager.STREAM_MUSIC, 60).also { tone -> tone.startTone(ToneGenerator.TONE_PROP_BEEP, 120); main.postDelayed({ tone.release() }, 200) } } catch (_: Throwable) {}
        post {
            VoiceBus.listener?.onWake()
            val c = client
            if (c?.isRunning == true) { c.interrupt(); lastActivityAt = SystemClock.elapsedRealtime() }
            else openSession()
        }
    }

    private fun stopMic() {
        micRunning = false
        try { micThread?.join(800) } catch (_: Throwable) {}
        if (micThread?.isAlive != true) micThread = null
        // The mic thread owns and closes inference sessions after capture stops.
    }

    // ---- plumbing ---------------------------------------------------------

    private fun post(r: () -> Unit) { main.post(r) }

    private fun setState(s: String) {
        VoiceBus.state = s; VoiceBus.listener?.onState(s)
        (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager).notify(NOTIF_ID, buildNotification(s))
    }

    private fun ensureForeground() {
        startForegroundCompat(buildNotification("starting…"))
        if (wakeLock == null) {
            val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "rook:voice").also { it.acquire() }
        }
    }

    override fun onDestroy() { inst = null; standby = false; closeSession(); stopMic(); try { wakeLock?.release() } catch (_: Throwable) {}; wakeLock = null; super.onDestroy() }
    override fun onBind(intent: Intent?): IBinder? = null

    private fun startForegroundCompat(n: Notification) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        else startForeground(NOTIF_ID, n)
    }

    private fun stopForegroundCompat() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) stopForeground(STOP_FOREGROUND_REMOVE)
        else @Suppress("DEPRECATION") stopForeground(true)
    }

    private fun buildNotification(text: String): Notification {
        fun pi(id: Int, action: String) = PendingIntent.getService(this, id,
            Intent(this, VoiceService::class.java).setAction(action), piFlags())
        val b = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) Notification.Builder(this, CHANNEL)
                else @Suppress("DEPRECATION") Notification.Builder(this)
        return b.setContentTitle("Rook voice · $text")
            .setContentText(if (text == "standby") "say \"hey sojourn\"" else url)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setOngoing(true)
            .addAction(android.R.drawable.ic_btn_speak_now, "Talk", pi(3, ACTION_SESSION))
            .addAction(android.R.drawable.ic_media_pause, "Interrupt", pi(1, ACTION_INTERRUPT))
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, "Off", pi(2, ACTION_STOP))
            .build()
    }

    private fun piFlags() = PendingIntent.FLAG_UPDATE_CURRENT or
        (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) PendingIntent.FLAG_IMMUTABLE else 0)

    companion object {
        @Volatile var inst: VoiceService? = null
        private const val TAG = "VoiceService"
        const val CHANNEL = "rook_voice"
        const val NOTIF_ID = 2
        const val WAKE_MODEL = "hey_sojourn.onnx"
        const val WAKE_THRESHOLD = 0.5f
        const val WAKE_REFRACTORY_MS = 2000L
        const val IDLE_CLOSE_MS = 300_000L
        const val ACTION_STANDBY = "systems.bake.rook.voice.STANDBY"
        const val ACTION_SESSION = "systems.bake.rook.voice.SESSION"
        const val ACTION_END_SESSION = "systems.bake.rook.voice.END_SESSION"
        const val ACTION_INTERRUPT = "systems.bake.rook.voice.INTERRUPT"
        const val ACTION_STOP = "systems.bake.rook.voice.STOP"
        const val EXTRA_URL = "url"
        const val EXTRA_INSECURE = "insecure"

        private fun fg(ctx: Context, i: Intent) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) ctx.startForegroundService(i) else ctx.startService(i)
        }
        private fun base(ctx: Context, url: String, insecure: Boolean) =
            Intent(ctx, VoiceService::class.java).putExtra(EXTRA_URL, url).putExtra(EXTRA_INSECURE, insecure)

        /** Mic on + wake word armed. */
        fun standby(ctx: Context, url: String, insecure: Boolean) = fg(ctx, base(ctx, url, insecure).setAction(ACTION_STANDBY))
        /** Open a session immediately (push-to-talk); also arms standby. */
        fun start(ctx: Context, url: String, insecure: Boolean) = fg(ctx, base(ctx, url, insecure).setAction(ACTION_SESSION))
        fun interrupt(ctx: Context) = ctx.startService(Intent(ctx, VoiceService::class.java).setAction(ACTION_INTERRUPT))
        fun endSession(ctx: Context) = ctx.startService(Intent(ctx, VoiceService::class.java).setAction(ACTION_END_SESSION))
        fun stop(ctx: Context) = ctx.startService(Intent(ctx, VoiceService::class.java).setAction(ACTION_STOP))

        /** Send a typed message (or image), starting the service if it isn't up yet. */
        fun send(ctx: Context, text: String, imageB64: String? = null, speak: Boolean = false) {
            val i = inst
            if (i != null) { i.submit(text, imageB64, speak); return }
            val prefs = ctx.getSharedPreferences("rook", MODE_PRIVATE)
            pending = Triple(text, imageB64, speak)
            fg(ctx, base(ctx, prefs.getString("voice_url", "") ?: "",
                         prefs.getBoolean("voice_insecure", false)).setAction(ACTION_STANDBY))
        }
        @Volatile var pending: Triple<String, String?, Boolean>? = null
    }
}

/** Tiny main-thread event bus so the Activity can mirror the session. */
object VoiceBus {
    interface Listener {
        fun onState(state: String)
        fun onTranscript(text: String)
        fun onAssistantDelta(text: String)
        fun onAssistantDone()
        fun onInterrupt()
        fun onError(msg: String)
        fun onWake() {}
        fun onBye(mode: String) {}
        fun onTool(title: String, status: String) {}
    }
    @Volatile var state: String = "idle"
    @Volatile var listener: Listener? = null
}

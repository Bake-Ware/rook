package systems.bake.rook

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.media.AudioRecordingConfiguration
import android.os.Build
import android.os.Handler
import android.os.SystemClock
import android.telephony.PhoneStateListener
import android.telephony.TelephonyManager
import android.util.Log

/**
 * Platform side of [MicYieldPolicy]: watches who else wants audio input and tells
 * VoiceService when to release its AudioRecord and when it may open it again.
 * Everything here runs on [main]; the capture thread only touches the volatile
 * [ownSessionId] / [ownsMode] fields.
 *
 * Signals, all version-guarded so minSdk 23 keeps working:
 *  - API 24+: AudioManager recording callback (another client recording);
 *    API 29+: AudioRecordingConfiguration.isClientSilenced for our own client.
 *  - Audio focus, held only during a voice conversation (LOSS / LOSS_TRANSIENT).
 *  - API 31+: audio mode changes; API 23-30: telephony call state (no permission
 *    needed below 31). Audio mode is also re-read on every evaluation.
 */
internal class MicArbiter(
    ctx: Context,
    private val main: Handler,
    private val release: (MicYieldPolicy.Reason) -> Unit,
    private val resume: () -> Unit,
    private val focusGone: () -> Unit,
    private val changed: (MicYieldPolicy.Reason?) -> Unit,
) {
    private val audio = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private val telephony = ctx.getSystemService(Context.TELEPHONY_SERVICE) as? TelephonyManager
    val policy = MicYieldPolicy()

    /** Session id of Rook's live AudioRecord, or 0 when none (set by the capture thread). */
    @Volatile var ownSessionId = 0
    /** True while Rook itself has put audio into MODE_IN_COMMUNICATION. */
    @Volatile var ownsMode = false
    /** Reason for the last release, read by the capture thread's cleanup. */
    @Volatile var yieldReason: MicYieldPolicy.Reason? = null
        private set

    private var started = false
    private var telephonyCall = false
    var focusHeld = false
        private set
    private var focusRequest: Any? = null
    private var recordingCallback: Any? = null
    private var modeListener: Any? = null
    private var phoneListener: PhoneStateListener? = null
    private var lastReported: MicYieldPolicy.Reason? = null
    private var reported = false

    val paused get() = policy.paused
    val pauseReason get() = if (policy.paused) policy.reason ?: yieldReason else null

    private val focusListener = AudioManager.OnAudioFocusChangeListener { change ->
        main.post {
            val lost = MicYieldPolicy.focusLoss(change) ?: return@post
            if (!focusHeld) return@post
            Log.i(TAG, "audio focus change=$change")
            policy.focusLost = lost
            evaluate()
            if (change == AudioManager.AUDIOFOCUS_LOSS) focusGone()
        }
    }

    private val retry = Runnable { evaluate() }

    fun start() {
        if (started) return
        started = true
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            val cb = object : AudioManager.AudioRecordingCallback() {
                override fun onRecordingConfigChanged(configs: MutableList<AudioRecordingConfiguration>?) { evaluate(configs) }
            }
            try { audio.registerAudioRecordingCallback(cb, main); recordingCallback = cb } catch (e: Exception) { Log.w(TAG, "recording callback", e) }
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            val l = AudioManager.OnModeChangedListener { main.post { evaluate() } }
            try { audio.addOnModeChangedListener({ it.run() }, l); modeListener = l } catch (e: Exception) { Log.w(TAG, "mode listener", e) }
        } else if (telephony != null) {
            @Suppress("DEPRECATION")
            val l = object : PhoneStateListener() {
                @Deprecated("Deprecated in Java")
                override fun onCallStateChanged(state: Int, phoneNumber: String?) {
                    telephonyCall = state != TelephonyManager.CALL_STATE_IDLE
                    evaluate()
                }
            }
            try {
                @Suppress("DEPRECATION") telephony.listen(l, PhoneStateListener.LISTEN_CALL_STATE)
                phoneListener = l
            } catch (e: Exception) { Log.w(TAG, "call state listener", e) }
        }
    }

    fun stop() {
        if (!started) return
        started = false
        main.removeCallbacks(retry)
        setFocusWanted(false)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) (recordingCallback as? AudioManager.AudioRecordingCallback)?.let {
            try { audio.unregisterAudioRecordingCallback(it) } catch (_: Exception) {}
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) (modeListener as? AudioManager.OnModeChangedListener)?.let {
            try { audio.removeOnModeChangedListener(it) } catch (_: Exception) {}
        }
        phoneListener?.let { @Suppress("DEPRECATION") try { telephony?.listen(it, PhoneStateListener.LISTEN_NONE) } catch (_: Exception) {} }
        recordingCallback = null; modeListener = null; phoneListener = null; telephonyCall = false
        idle()
    }

    /** Capture is no longer wanted: drop pause/backoff state. */
    fun idle() {
        main.removeCallbacks(retry)
        policy.reset()
        yieldReason = null
        report()
    }

    /** Re-read every signal; release or schedule re-acquire as the policy decides. */
    fun evaluate(configs: List<AudioRecordingConfiguration>? = null) {
        if (!started) return
        val now = refresh(configs)
        main.removeCallbacks(retry)
        if (policy.paused) {
            val at = policy.retryAt()
            if (at != null && now >= at) {
                Log.i(TAG, "re-acquiring mic")
                resume()
            } else {
                // Re-check periodically too: API 23 has no recording callback and
                // callbacks can be missed while the process is busy.
                main.postDelayed(retry, if (at != null) (at - now).coerceIn(50L, POLL_MS) else POLL_MS)
            }
        }
        report()
    }

    /** Whether the capture thread may open the mic right now (never re-enters [resume]). */
    fun mayCapture(): Boolean {
        if (!started) return true
        val now = refresh(null)
        val ok = policy.mayAcquire(now)
        if (!ok) evaluate()   // keep the retry timer and UI state current
        return ok
    }

    /** Read the signals and release capture if the policy says so. Returns now. */
    private fun refresh(configs: List<AudioRecordingConfiguration>?): Long {
        readRecorders(configs)
        policy.inCall = telephonyCall || MicYieldPolicy.callMode(currentMode(),
            rookOwnsMode = ownsMode || Build.VERSION.SDK_INT < Build.VERSION_CODES.S)
        val now = SystemClock.elapsedRealtime()
        if (policy.update(now)) {
            val r = policy.reason ?: MicYieldPolicy.Reason.OTHER_APP
            yieldReason = r
            Log.i(TAG, "yielding mic: $r (attempt ${policy.attempts})")
            release(r)
        }
        return now
    }

    /** Recording started successfully on the capture thread. */
    fun micStarted() {
        policy.acquired(SystemClock.elapsedRealtime())
        yieldReason = null
        evaluate()
    }

    /**
     * The capture thread failed. Returns true when this was a contended mic (we were
     * re-acquiring, or someone else is now recording): stay paused and retry later
     * instead of turning standby off.
     */
    fun micFailed(busy: Boolean): Boolean {
        if (!started) return false
        val wasResuming = policy.attempts > 0 || yieldReason != null
        readRecorders(null)
        if (!busy && !wasResuming && policy.reason == null) return false
        policy.acquireFailed(SystemClock.elapsedRealtime())
        if (yieldReason == null) yieldReason = policy.reason ?: MicYieldPolicy.Reason.OTHER_APP
        evaluate()
        return true
    }

    /** Hold audio focus only while a voice conversation is live (never in plain standby). */
    fun setFocusWanted(want: Boolean) {
        if (want == focusHeld) return
        if (want) {
            val granted = try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    val req = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
                        .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ASSISTANT)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                        .setOnAudioFocusChangeListener(focusListener, main)
                        .build()
                    focusRequest = req
                    audio.requestAudioFocus(req)
                } else {
                    @Suppress("DEPRECATION")
                    audio.requestAudioFocus(focusListener, AudioManager.STREAM_VOICE_CALL, AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
                } == AudioManager.AUDIOFOCUS_REQUEST_GRANTED
            } catch (e: Exception) { Log.w(TAG, "focus request", e); true }
            // Denied (e.g. during a call): hold nothing; call/recorder signals still apply.
            if (!granted) Log.i(TAG, "audio focus denied")
            focusHeld = granted
            policy.focusLost = false
        } else {
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) (focusRequest as? AudioFocusRequest)?.let { audio.abandonAudioFocusRequest(it) }
                else @Suppress("DEPRECATION") audio.abandonAudioFocus(focusListener)
            } catch (_: Exception) {}
            focusRequest = null
            focusHeld = false
            policy.focusLost = false
        }
        evaluate()
    }

    private fun currentMode() = try { audio.mode } catch (_: Exception) { AudioManager.MODE_NORMAL }

    private fun readRecorders(given: List<AudioRecordingConfiguration>?) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.N) return
        val configs = given ?: try { audio.activeRecordingConfigurations } catch (_: Exception) { return }
        val own = ownSessionId
        var others = false
        var silenced = false
        for (c in configs) {
            val mine = own != 0 && c.clientAudioSessionId == own
            if (mine) {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q && c.isClientSilenced) silenced = true
            } else if (c.clientAudioSource != MicYieldPolicy.SOURCE_HOTWORD) {
                others = true
            }
        }
        policy.othersRecording = others
        policy.silenced = silenced
    }

    private fun report() {
        val r = pauseReason
        if (reported && r == lastReported) return
        reported = true; lastReported = r
        changed(r)
    }

    companion object {
        private const val TAG = "MicArbiter"
        private const val POLL_MS = 5_000L
    }
}

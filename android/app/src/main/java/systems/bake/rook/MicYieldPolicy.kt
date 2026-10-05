package systems.bake.rook

/**
 * Decides when Rook must give the microphone back to the rest of the phone, and
 * when it may take it again. Pure Kotlin (no Android types) so it is unit-tested
 * on the JVM; VoiceService feeds it platform signals and acts on its answers.
 *
 * Yield triggers (any one is enough):
 *  - another client is recording (AudioManager recording callback, API 24+)
 *  - our own recorder was silenced by the platform (isClientSilenced, API 29+)
 *  - audio focus lost (AUDIOFOCUS_LOSS / AUDIOFOCUS_LOSS_TRANSIENT; never CAN_DUCK)
 *  - a call is ringing or in progress (audio mode / telephony call state)
 *
 * While yielded, capture is fully released. It is re-acquired only once every
 * trigger has cleared, after a settle delay that backs off exponentially when we
 * keep getting pushed off the mic (or the recorder fails to open), so Rook never
 * grabs the mic back while another app is still using it.
 */
internal class MicYieldPolicy(
    private val settleMs: Long = 1_500L,
    private val maxBackoffMs: Long = 30_000L,
    private val stableMs: Long = 20_000L,
) {
    enum class Reason { CALL, OTHER_APP, FOCUS }

    var othersRecording = false
    var silenced = false
    var inCall = false
    var focusLost = false

    /** True while capture is released because of [reason]. */
    var paused = false
        private set
    /** Consecutive yields/failures soon after re-acquiring; drives the backoff. */
    var attempts = 0
        private set
    private var clearSince = -1L
    private var acquiredAt = -1L

    /** The current yield reason, or null when nothing else wants the mic. */
    val reason: Reason?
        get() = when {
            inCall -> Reason.CALL
            othersRecording || silenced -> Reason.OTHER_APP
            focusLost -> Reason.FOCUS
            else -> null
        }

    /** Re-evaluate after any signal changed. True means: release capture now. */
    fun update(now: Long): Boolean {
        val r = reason
        if (r != null) {
            clearSince = -1L
            if (paused) return false
            paused = true
            if (acquiredAt >= 0 && now - acquiredAt < stableMs) attempts++ else attempts = 0
            acquiredAt = -1L
            return true
        }
        if (paused && clearSince < 0) clearSince = now
        return false
    }

    /** Delay before the next re-acquire attempt; only meaningful while paused. */
    fun backoffMs(): Long {
        var d = settleMs
        repeat(attempts.coerceAtMost(16)) { d = (d * 2).coerceAtMost(maxBackoffMs) }
        return d.coerceAtMost(maxBackoffMs)
    }

    /** When capture may be re-acquired, or null while still yielded / not paused. */
    fun retryAt(): Long? = if (paused && reason == null && clearSince >= 0) clearSince + backoffMs() else null

    /** True when capture may be (re)opened at [now]. */
    fun mayAcquire(now: Long): Boolean {
        if (reason != null) return false
        if (!paused) return true
        val at = retryAt() ?: return false
        return now >= at
    }

    /** The recorder opened and is recording: capture is held again. */
    fun acquired(now: Long) {
        paused = false
        clearSince = -1L
        acquiredAt = now
    }

    /** The recorder could not be opened after [tryAcquire]: stay paused, back off further. */
    fun acquireFailed(now: Long) {
        paused = true
        attempts++
        acquiredAt = -1L
        clearSince = if (reason == null) now else -1L
    }

    /** Capture is no longer wanted at all: forget pause state and backoff. */
    fun reset() {
        paused = false; attempts = 0; clearSince = -1L; acquiredAt = -1L
        othersRecording = false; silenced = false
    }

    companion object {
        // AudioManager.MODE_* values (stable platform constants), kept literal so this stays pure.
        const val MODE_RINGTONE = 1
        const val MODE_IN_CALL = 2
        const val MODE_IN_COMMUNICATION = 3
        const val MODE_CALL_SCREENING = 4
        const val MODE_CALL_REDIRECT = 5
        const val MODE_COMMUNICATION_REDIRECT = 6

        /**
         * Whether an audio [mode] means a call owns audio. MODE_IN_COMMUNICATION is
         * ambiguous because Rook itself selects it while capturing; it only counts
         * when Rook did not set it.
         */
        fun callMode(mode: Int, rookOwnsMode: Boolean): Boolean = when (mode) {
            MODE_RINGTONE, MODE_IN_CALL, MODE_CALL_SCREENING, MODE_CALL_REDIRECT, MODE_COMMUNICATION_REDIRECT -> true
            MODE_IN_COMMUNICATION -> !rookOwnsMode
            else -> false
        }

        /** Audio focus change codes (AudioManager.AUDIOFOCUS_*): which ones mean "yield". */
        fun focusLoss(change: Int): Boolean? = when (change) {
            -1, -2 -> true      // LOSS, LOSS_TRANSIENT
            -3 -> null          // LOSS_TRANSIENT_CAN_DUCK: keep listening, no change
            1, 2, 3, 4 -> false // GAIN, GAIN_TRANSIENT[_MAY_DUCK|_EXCLUSIVE]
            else -> null
        }

        /** AudioSource.HOTWORD (hidden constant): always-on system hotword capture never blocks Rook. */
        const val SOURCE_HOTWORD = 1999
    }
}

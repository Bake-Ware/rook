package systems.bake.rook

/**
 * When a spoken reminder (attention.remind) must not be spoken, as pure logic (no Android
 * types) so it is unit-tested on the JVM. [SpokenReminder] gathers the platform signals.
 * The outcome names are the contract's (`reminded` events in attention.poll).
 */
object ReminderGate {
    const val SPOKEN = "spoken"
    const val BUSY = "busy"
    const val CALL = "call"
    const val VOICE_SESSION = "voice_session"
    const val MIC_IN_USE = "mic_in_use"
    const val MEDIA = "media"
    const val SPEAKING = "speaking"
    const val UNCHECKED = "unchecked"
    const val OFF = "off"
    const val ERROR = "error"

    /** Limits shared with attention_android.py (it validates first; these are a second guard). */
    const val ID_MAX = 64
    const val TEXT_MAX = 300
    const val SAMPLE_MIN_MS = 1_000
    const val SAMPLE_MAX_MS = 8_000

    fun clampSample(ms: Int): Int = ms.coerceIn(SAMPLE_MIN_MS, SAMPLE_MAX_MS)

    /** One audio input client, as AudioManager.getActiveRecordingConfigurations reports it. */
    data class Recorder(val sessionId: Int, val source: Int, val silenced: Boolean)

    /** What the recorders say: someone other than Rook records; Rook's own [mine] client is silenced. */
    data class Recorders(val others: Boolean, val mineSilenced: Boolean)

    /**
     * [own] are Rook's own live sessions (wake standby's recorder, the quiet check's), [mine] the
     * quiet check's own one. The system's always-on hotword capture never counts as another app.
     */
    fun recorders(list: List<Recorder>, own: Set<Int>, mine: Int = 0): Recorders {
        var others = false
        var silenced = false
        for (r in list) {
            val ours = r.sessionId != 0 && r.sessionId in own
            if (ours) { if (mine != 0 && r.sessionId == mine && r.silenced) silenced = true }
            else if (r.source != MicYieldPolicy.SOURCE_HOTWORD) others = true
        }
        return Recorders(others, silenced)
    }

    /** Everything the pre-checks look at, read fresh before the sample and while it runs. */
    data class Signals(
        /** AudioManager.getMode(). */
        val audioMode: Int = 0,
        /** MODE_IN_COMMUNICATION was set by Rook's own voice conversation, not by a call. */
        val rookOwnsCallMode: Boolean = false,
        /** TelephonyManager call state is ringing or off-hook (readable below Android 12). */
        val telephonyCall: Boolean = false,
        /** Rook's voice conversation is live, or a voice session is open. */
        val voiceSession: Boolean = false,
        /** VoiceBus.state (idle, standby, listening, thinking, speaking, ...). */
        val voiceState: String = "idle",
        /** Another app is recording (Rook's own sessions excluded). */
        val othersRecording: Boolean = false,
        /** AudioManager.isMusicActive. */
        val musicActive: Boolean = false,
        /** Rook's voice.speak queue or reply window is busy. */
        val speaking: Boolean = false,
    )

    /**
     * The first reason not to speak, in the contract's order, or null when the pre-checks pass.
     * Rook's own speech also makes the music stream active; that is reported as `speaking`.
     */
    fun precheck(s: Signals): String? = when {
        s.telephonyCall || MicYieldPolicy.callMode(s.audioMode, s.rookOwnsCallMode) -> CALL
        s.voiceSession || s.voiceState == "thinking" || s.voiceState == "speaking" -> VOICE_SESSION
        s.othersRecording -> MIC_IN_USE
        s.musicActive && !s.speaking -> MEDIA
        s.speaking -> SPEAKING
        else -> null
    }

    /** After the sample: null means speak now, otherwise the outcome that ends the reminder. */
    fun afterSample(outcome: QuietCheck.Outcome?, speakUnchecked: Boolean): String? = when (outcome) {
        QuietCheck.Outcome.QUIET -> null
        QuietCheck.Outcome.BUSY -> BUSY
        QuietCheck.Outcome.SILENCED, null -> if (speakUnchecked) null else UNCHECKED
    }

    /**
     * The sample was cut short (wake standby stopped feeding frames): enough audio still decides;
     * otherwise a pre-check that now fires explains why, else the room could not be checked.
     */
    fun shortSample(measuredMs: Int, wantedMs: Int): Boolean = measuredMs * 10 < wantedMs * 6
}

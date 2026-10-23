package systems.bake.rook

/**
 * Local barge-in gate while the assistant is talking. Pure Kotlin (JVM-tested);
 * VoiceClient owns the AudioTrack and socket and acts on each answer.
 *
 * Before confirmation (both server kinds): pause playback after [PAUSE_FRAMES] speech
 * frames, unpause after [QUIET_FRAMES] quiet ones. At [BARGE_FRAMES] sustained speech:
 *  - legacy server: BARGE (flush, speech_start, wait for the server's interrupt);
 *  - server that echoed "barge": "confirm": HOLD (stay paused, keep the queue, speech_start
 *    hold=true, stream the mic) until the server says resume or interrupt. Held longer
 *    than [holdMs] with neither is handled as a local interrupt (see [expired]).
 */
internal class BargeGate(private val holdMs: Long = HOLD_MS) {
    enum class Frame { SEND, DROP, PAUSE, UNPAUSE, BARGE, HOLD }
    companion object {
        const val PAUSE_FRAMES = 8; const val BARGE_FRAMES = 18; const val QUIET_FRAMES = 10
        const val HOLD_MS = 10_000L
        /** playingUntil after a pause from [pausedAt] to [now]: the track did not advance meanwhile. */
        fun resumedUntil(playingUntil: Long, pausedAt: Long, now: Long) = playingUntil + (now - pausedAt)
        /** playingUntil after queueing [ms] more audio; while paused the clock stopped at [pausedAt]. */
        fun extendedUntil(playingUntil: Long, now: Long, pausedAt: Long?, ms: Long) = maxOf(playingUntil, pausedAt ?: now) + ms
    }
    /** The session echoed "barge": "confirm". */
    @Volatile var confirm = false
    var held = false; private set
    var heldSince = 0L; private set
    private var speechFrames = 0
    private var quietFrames = 0

    /** One 20 ms mic frame. [playing]: assistant audio is due; [gated]: AEC on and protocol >= 2. */
    fun frame(speech: Boolean, playing: Boolean, paused: Boolean, gated: Boolean, now: Long): Frame {
        if (held) return Frame.SEND
        if (!playing && !paused) { speechFrames = 0; quietFrames = 0; return Frame.SEND }
        // Send no echo-bearing audio until the local speech gate confirms barge-in.
        if (!gated) return Frame.DROP
        if (speech) { speechFrames++; quietFrames = 0 } else { quietFrames++; speechFrames = 0 }
        if (speechFrames >= BARGE_FRAMES) {
            speechFrames = 0; quietFrames = 0
            if (!confirm) return Frame.BARGE
            held = true; heldSince = now
            return Frame.HOLD
        }
        if (speechFrames >= PAUSE_FRAMES && !paused) return Frame.PAUSE
        if (paused && quietFrames >= QUIET_FRAMES) return Frame.UNPAUSE
        return Frame.DROP
    }
    /** Server said resume: true when a hold ended (the caller unpauses playback). */
    fun resume(): Boolean { if (!held) return false; held = false; return true }
    /** True once when a hold outlived [holdMs]: the caller flushes as if interrupted. */
    fun expired(now: Long): Boolean {
        if (!held || now - heldSince <= holdMs) return false
        held = false; return true
    }
    /** Interrupt or flush: whatever was held is gone. */
    fun clear() { held = false }
}

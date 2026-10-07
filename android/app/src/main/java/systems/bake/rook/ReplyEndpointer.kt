package systems.bake.rook

/**
 * Decides when a voice.speak reply window ends, one 20 ms frame at a time
 * (docs/design/voice-replies.md). Pure, so it is unit-tested on the JVM.
 *
 * The first [CALIBRATE_MS] only measure the room. Speech is then a frame louder
 * than both [MIN_LEVEL] and 3x the quietest frame seen before speech (the floor). A reply needs [MIN_SPEECH_MS] of speech in a
 * row; shorter blips (a door, a cough) don't open it. Once open, [endSilenceMs]
 * of quiet ends it. Nothing within [startTimeoutMs] means no reply; [maxMs] caps it.
 */
class ReplyEndpointer(
    private val startTimeoutMs: Int,
    private val endSilenceMs: Int = 1200,
    private val maxMs: Int = 20_000,
    private val frameMs: Int = 20,
) {
    enum class State { LISTENING, SPEECH_ENDED, NO_SPEECH, TOO_LONG }

    var heardSpeech = false
        private set
    private var elapsed = 0
    private var run = 0
    private var quiet = 0
    private var floor = Float.MAX_VALUE

    /** [level]: the frame's RMS on a 0..1 scale. */
    fun feed(level: Float): State {
        elapsed += frameMs
        if (elapsed <= CALIBRATE_MS) { floor = minOf(floor, maxOf(level, 1e-5f)); return State.LISTENING }
        // The floor is capped so talking straight away (the "floor" is then speech) still counts.
        val threshold = maxOf(MIN_LEVEL, if (floor == Float.MAX_VALUE) MIN_LEVEL else minOf(floor, MAX_FLOOR) * 3f)
        if (level >= threshold) {
            run += frameMs; quiet = 0
            if (run >= MIN_SPEECH_MS) heardSpeech = true
        } else {
            if (!heardSpeech) { run = 0; floor = minOf(floor, maxOf(level, 1e-5f)) }
            else quiet += frameMs
        }
        return when {
            heardSpeech && quiet >= endSilenceMs -> State.SPEECH_ENDED
            elapsed >= maxMs -> if (heardSpeech) State.TOO_LONG else State.NO_SPEECH
            !heardSpeech && run == 0 && elapsed >= startTimeoutMs -> State.NO_SPEECH
            else -> State.LISTENING
        }
    }

    companion object {
        const val MIN_LEVEL = 0.01f       // about -40 dBFS
        const val MIN_SPEECH_MS = 200
        const val CALIBRATE_MS = 100
        const val MAX_FLOOR = 0.03f       // about -30 dBFS
    }
}

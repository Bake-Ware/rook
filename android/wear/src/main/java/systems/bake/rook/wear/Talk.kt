package systems.bake.rook.wear

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import systems.bake.rook.wearlink.Transcript

/**
 * What the watch knows: [TalkService] writes the facts, [TalkActivity] and [TalkTileService]
 * draw them. The phase (the Rook icon's color) is derived from the facts and stored nowhere
 * else, so the button, the tile and the tap action always agree.
 */
object Talk {
    enum class Phase(val color: Long, val spoken: String) {
        IDLE(0xFF8E8E93, "Rook. Tap to talk"),
        CONNECTING(0xFF5E5CE6, "Rook is connecting. Tap to cancel"),
        LISTENING(0xFF2FD3A0, "Rook is listening. Tap to end"),
        THINKING(0xFFFFB340, "Rook is thinking. Tap to stop"),
        SPEAKING(0xFF0A84FF, "Rook is speaking. Tap to stop"),
        ERROR(0xFFFF453A, "Rook stopped. Tap to try again"),
    }

    data class State(
        /** A conversation is open ([TalkService] holds the mic). */
        val running: Boolean = false,
        /** The phone answered: mic packets flow. */
        val ready: Boolean = false,
        /** The voice server's last state (listening, thinking, speaking). */
        val server: String = "",
        /** Reply audio is coming out of the watch right now. */
        val playing: Boolean = false,
        /** Why the last conversation ended badly; cleared when the next one starts. */
        val failure: String = "",
        val transcript: Transcript = Transcript(),
    ) {
        val phase: Phase get() = phaseOf(this)
        val active get() = running

        /** The conversation is over; a [failure] turns the Rook red and is said in the transcript. */
        fun ended(failure: String?) = State(failure = failure ?: "",
            transcript = transcript.done().let { if (failure != null) it.note(failure) else it })
    }

    fun phaseOf(s: State): Phase = when {
        !s.running -> if (s.failure.isNotEmpty()) Phase.ERROR else Phase.IDLE
        // What the watch plays wins: the server is done long before the last words come out.
        s.playing -> Phase.SPEAKING
        !s.ready -> Phase.CONNECTING
        s.server == "thinking" -> Phase.THINKING
        s.server == "speaking" -> Phase.SPEAKING
        s.server == "listening" -> Phase.LISTENING
        else -> Phase.CONNECTING
    }

    private val flow = MutableStateFlow(State())
    val state: StateFlow<State> = flow

    fun update(change: (State) -> State) = flow.update(change)
}

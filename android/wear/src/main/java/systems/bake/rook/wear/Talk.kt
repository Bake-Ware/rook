package systems.bake.rook.wear

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update

/** What the watch shows: [TalkService] writes it, [TalkActivity] draws it. */
object Talk {
    enum class Phase { IDLE, CONNECTING, LISTENING, THINKING, SPEAKING, ERROR }

    data class State(
        val phase: Phase = Phase.IDLE,
        val heard: String = "",
        val reply: String = "",
        val error: String = "",
    ) {
        val active get() = phase != Phase.IDLE && phase != Phase.ERROR
    }

    private val flow = MutableStateFlow(State())
    val state: StateFlow<State> = flow

    fun update(change: (State) -> State) = flow.update(change)

    /** The voice server's state names (VoiceClient.Listener.onState). */
    fun phaseOf(serverState: String): Phase = when (serverState) {
        "listening" -> Phase.LISTENING
        "thinking" -> Phase.THINKING
        "speaking" -> Phase.SPEAKING
        else -> Phase.CONNECTING
    }
}

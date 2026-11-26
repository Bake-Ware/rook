package systems.bake.rook.core.worker

import android.content.Context
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/** Where the phone's worker is. */
sealed interface WorkerState {
    data object Stopped : WorkerState
    data object Joining : WorkerState
    data class Linked(val moniker: String) : WorkerState
    data class Failed(val message: String) : WorkerState
}

/**
 * The phone's worker, as the app sees it. This is the contract only:
 * the stub does nothing and stays Stopped. AP2 fills it in.
 */
object RookWorker {
    private val _state = MutableStateFlow<WorkerState>(WorkerState.Stopped)

    val state: StateFlow<WorkerState> = _state.asStateFlow()

    /** Start the worker for this hub; joinCode only the first time (null afterwards). */
    fun start(context: Context, hubUrl: String, joinCode: String?) {}

    fun stop(context: Context) {}
}

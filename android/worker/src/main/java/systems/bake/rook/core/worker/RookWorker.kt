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
 * The phone's worker, as the app sees it: the repo's own Python worker, run by [WorkerService].
 *
 * [start] keeps the hub's address (and the join code, until the worker has joined with it) in the
 * app's private preferences and starts the service, which runs the worker until [stop]; after a
 * reboot or an app update [BootReceiver] starts it again with what was kept. [state] is what the
 * worker last said: Joining while it knocks, links or links again, Linked with the moniker the hub
 * gave it, Failed with the worker's own words when it stopped on an error.
 */
object RookWorker {
    private val _state = MutableStateFlow<WorkerState>(WorkerState.Stopped)

    val state: StateFlow<WorkerState> = _state.asStateFlow()

    /** Start the worker for this hub; joinCode only the first time (null afterwards). */
    fun start(context: Context, hubUrl: String, joinCode: String?) {
        val prefs = prefs(context)
        prefs.edit().putString(HUB, hubUrl).putBoolean(ON, true).apply {
            if (joinCode != null) putString(CODE, joinCode)
        }.apply()
        WorkerService.start(context)
        Bridge.askToNotify(context)
    }

    fun stop(context: Context) {
        prefs(context).edit().putBoolean(ON, false).apply()
        WorkerService.stop(context)
    }

    internal fun told(state: WorkerState) {
        _state.value = state
    }

    internal fun prefs(context: Context) = context.getSharedPreferences("rook-worker", Context.MODE_PRIVATE)

    internal const val HUB = "hub"
    internal const val CODE = "code"
    internal const val ON = "on"
}

package systems.bake.rook

/**
 * Remembers the audio mode / speakerphone state from before Rook forced
 * MODE_IN_COMMUNICATION (and possibly speakerphone) for capture, and decides
 * what to put back when capture stops. Pure Kotlin so it is unit-tested on the JVM.
 *
 * Below Android 12 mode and speakerphone are global, so getting this wrong leaks
 * into the next phone call. Rules:
 *  - Speakerphone: if Rook turned it on, always put back the pre-Rook value, even
 *    after yielding to a call or another app (otherwise it stays stuck on).
 *  - Mode: restore while the mode is still Rook's MODE_IN_COMMUNICATION; leave it
 *    alone only when someone else has moved it (IN_CALL, RINGTONE, ...).
 *  - Rook's own forced state is never saved as the "old" state: while a previous
 *    capture's state has not been fully put back, the earlier saved values are kept.
 */
internal class AudioRouteKeeper {
    data class Saved(val mode: Int, val speaker: Boolean)
    /** What to write back: null fields mean "leave as is". */
    data class Restore(val mode: Int?, val speaker: Boolean?)

    private var saved: Saved? = null
    private var forcedSpeaker = false

    /** Capture is starting with the platform currently at [mode] / [speaker]. Returns the pre-Rook state. */
    @Synchronized fun begin(mode: Int, speaker: Boolean): Saved {
        saved?.let { return it }   // leftover from an unfinished restore: current values may be Rook's
        val s = Saved(if (mode == MicYieldPolicy.MODE_IN_COMMUNICATION) MODE_NORMAL else mode, speaker)
        saved = s
        forcedSpeaker = false
        return s
    }

    /** Rook switched speakerphone on for this capture. */
    @Synchronized fun speakerForced() { forcedSpeaker = true }

    /** Capture stopped and the platform mode is now [currentMode]. */
    @Synchronized fun end(currentMode: Int): Restore {
        val s = saved ?: return Restore(null, null)
        return Restore(
            mode = if (currentMode == MicYieldPolicy.MODE_IN_COMMUNICATION) s.mode else null,
            speaker = if (forcedSpeaker) s.speaker else null,
        )
    }

    /** The [end] decision was applied successfully; the next capture saves fresh values. */
    @Synchronized fun restored() { saved = null; forcedSpeaker = false }

    companion object {
        const val MODE_NORMAL = 0
    }
}

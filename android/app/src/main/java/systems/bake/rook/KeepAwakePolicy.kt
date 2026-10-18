package systems.bake.rook

/**
 * When keep-awake holds the screen, as plain state so it can be unit tested.
 *
 * Two independent deadlines: an explicit hold (device.keep_awake on, for N
 * minutes) and the passive idle timer that every screen-driving cap call
 * (ui.*, hid.*, screenshot.*, ...) pushes forward. The screen is held until the
 * later of the two; off clears both.
 */
class KeepAwakePolicy {
    var manualUntil = 0L
        private set
    var autoUntil = 0L
        private set

    /** Explicit hold for [minutes] from [now]; replaces any earlier explicit hold. */
    fun engage(now: Long, minutes: Int) {
        manualUntil = now + clampMinutes(minutes) * 60_000L
    }

    /**
     * A screen-driving call at [now]: restart the idle timer. Returns true when
     * this call starts a hold (nothing was held before), so the caller wakes the
     * screen and may unlock it.
     */
    fun activity(now: Long, idleMinutes: Int): Boolean {
        val started = !held(now)
        autoUntil = now + clampMinutes(idleMinutes) * 60_000L
        return started
    }

    fun clearAuto() { autoUntil = 0L }

    fun release() { manualUntil = 0L; autoUntil = 0L }

    fun until(): Long = maxOf(manualUntil, autoUntil)

    fun held(now: Long): Boolean = until() > now

    /** "manual", "auto" (whichever lasts longer), or null when not held. */
    fun mode(now: Long): String? = when {
        !held(now) -> null
        manualUntil >= autoUntil -> "manual"
        else -> "auto"
    }

    companion object {
        const val MAX_MINUTES = 24 * 60
        fun clampMinutes(minutes: Int): Int = minutes.coerceIn(1, MAX_MINUTES)
    }
}

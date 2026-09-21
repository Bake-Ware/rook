package systems.bake.rook

/** Main-thread capture ownership, shared by command, timeout, retry and background paths. */
internal class VoiceCaptureLifetime(private val releaseCapture: () -> Unit) {
    var sessionWanted = false
    var voiceSession = false
    @Volatile var standby = false
    @Volatile var wakeEnabled = true
    val wakeStandby get() = standby && wakeEnabled
    val needsCapture get() = (sessionWanted && voiceSession) || wakeStandby

    fun endSession() {
        sessionWanted = false
        voiceSession = false
        reconcile()
    }

    fun reconcile() {
        if (!needsCapture) releaseCapture()
    }
}

/** Each resource is independent: an effect or inference failure must never leak capture. */
internal fun cleanupVoiceCapture(
    stop: () -> Unit, release: () -> Unit, before: List<() -> Unit>, after: List<() -> Unit>
) {
    fun safely(action: () -> Unit) { try { action() } catch (_: Exception) {} }
    try {
        before.forEach { safely(it) }
    } finally {
        try { safely(stop) } finally { safely(release) }
        after.forEach { safely(it) }
    }
}

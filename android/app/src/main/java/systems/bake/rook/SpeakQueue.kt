package systems.bake.rook

/**
 * Bookkeeping for voice.speak: which utterances are waiting, which were handed
 * to the TTS engine, and how each one ended. Pure (no Android types) so the
 * ordering rules are unit-tested on the JVM; [SpeakBridge] owns the engine.
 *
 * Rules:
 *  - Jobs leave the queue in FIFO order.
 *  - While the app's voice session is playing a reply, a normal job waits; an
 *    `interrupt` job goes anyway (the bridge stops the reply first).
 *  - An `interrupt` job supersedes everything queued or playing before it.
 *  - Finished jobs are kept (bounded) so callers can poll their state.
 */
class SpeakQueue(private val keep: Int = 64) {
    class Job(val id: String, val text: String, val voice: String, val rate: Float,
              val pitch: Float, val interrupt: Boolean) {
        @Volatile var state: String = QUEUED
        @Volatile var error: String? = null
        @Volatile var note: String? = null
        @Volatile var via: String? = null        // "server" (voice server) or "device" (on-device TTS)
        val done get() = state in FINAL
    }

    private val pending = ArrayDeque<Job>()
    private val inFlight = LinkedHashMap<String, Job>()
    private val jobs = LinkedHashMap<String, Job>()

    @Synchronized fun add(job: Job): Job {
        pending.addLast(job); jobs[job.id] = job
        while (jobs.size > keep) {
            val oldest = jobs.entries.firstOrNull { it.value.done } ?: break
            jobs.remove(oldest.key)
        }
        return job
    }

    /** The next job to hand to the engine now, or null if nothing may play yet. */
    @Synchronized fun next(replyPlaying: Boolean): Job? {
        val head = pending.firstOrNull() ?: return null
        // A later interrupt job jumps ahead of earlier jobs that are being held back.
        val pick = if (replyPlaying) pending.firstOrNull { it.interrupt } ?: return null else head
        if (pick.interrupt) {
            // Supersede everything older: held-back and already-submitted speech.
            for (j in pending.toList()) {
                if (j === pick) break
                pending.remove(j); end(j, STOPPED, "superseded by an interrupting voice.speak")
            }
            for (j in inFlight.values.toList()) end(j, STOPPED, "superseded by an interrupting voice.speak")
            inFlight.clear()
        }
        pending.remove(pick)
        pick.state = SUBMITTED
        inFlight[pick.id] = pick
        return pick
    }

    @Synchronized fun started(id: String) { inFlight[id]?.let { if (!it.done) it.state = SPEAKING } }

    /** Records the outcome. Returns true when nothing is playing or waiting any more. */
    @Synchronized fun finished(id: String, state: String, error: String? = null): Boolean {
        val j = inFlight.remove(id) ?: jobs[id]
        if (j != null && !j.done) end(j, state, error)
        return idle()
    }

    /** Fails every job that has not finished (engine init failed, engine died). */
    @Synchronized fun failAll(error: String) {
        for (j in pending) end(j, ERROR, error)
        for (j in inFlight.values) end(j, ERROR, error)
        pending.clear(); inFlight.clear()
    }

    /**
     * The engine died or rejected an utterance: everything already handed to it is
     * lost, so fail every in-flight job. Pending jobs stay queued for a rebuilt engine.
     * Returns the number of jobs failed.
     */
    @Synchronized fun engineLost(error: String): Int {
        val n = inFlight.size
        for (j in inFlight.values) end(j, ERROR, error)
        inFlight.clear()
        return n
    }

    @Synchronized fun stopAll(): Int {
        val n = pending.size + inFlight.size
        for (j in pending) end(j, STOPPED, "stopped by voice.speak_stop")
        for (j in inFlight.values) end(j, STOPPED, "stopped by voice.speak_stop")
        pending.clear(); inFlight.clear()
        return n
    }

    @Synchronized fun idle() = pending.isEmpty() && inFlight.isEmpty()
    @Synchronized fun hasPending() = pending.isNotEmpty()
    @Synchronized fun hasPendingInterrupt() = pending.any { it.interrupt }
    @Synchronized fun get(id: String): Job? = jobs[id]
    @Synchronized fun ahead(id: String): Int {
        val i = pending.indexOfFirst { it.id == id }
        return if (i < 0) 0 else i + inFlight.size
    }

    private fun end(j: Job, state: String, error: String?) {
        j.state = state
        if (error != null && j.error == null) j.error = error
    }

    companion object {
        const val QUEUED = "queued"        // waiting (engine starting, or a voice reply is playing)
        const val SUBMITTED = "submitted"  // handed to the TTS engine
        const val SPEAKING = "speaking"
        const val DONE = "done"
        const val STOPPED = "stopped"
        const val ERROR = "error"
        val FINAL = setOf(DONE, STOPPED, ERROR)
    }
}

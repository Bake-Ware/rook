package systems.bake.rook

import org.json.JSONArray
import org.json.JSONObject

/** A `timer` event from the voice server (docs/design/voice-front-background.md). */
data class TimerEvent(val action: String, val id: String, val label: String, val firesAt: Long, val durationS: Long) {
    companion object {
        private const val MAX_ID = 128
        private const val MAX_LABEL = 80
        private fun JSONObject.whole(key: String): Long? = (opt(key) as? Number)?.toDouble()
            ?.takeIf { it.isFinite() && it >= 0 && it < Long.MAX_VALUE.toDouble() }?.toLong()

        fun parse(m: JSONObject): TimerEvent? {
            if (m.optString("type") != "timer") return null
            val action = m.optString("action").takeIf { it == "set" || it == "cancel" } ?: return null
            val id = (m.opt("id") as? String)?.trim()?.takeIf { it.isNotEmpty() && it.length <= MAX_ID } ?: return null
            val label = ((m.opt("label") as? String) ?: "").trim().take(MAX_LABEL)
            val firesAt = m.whole("fires_at") ?: 0L
            val durationS = m.whole("duration_s") ?: 0L
            if (action == "set" && firesAt <= 0 && durationS <= 0) return null
            return TimerEvent(action, id, label, firesAt, durationS)
        }
    }
}

/**
 * An armed timer. [firesAt] is the phone-clock time it rings; [serverFiresAt] is the
 * `fires_at` the server sent, kept as the resend identity (see [TimerBook]).
 */
data class ActiveTimer(val id: String, val label: String, val firesAt: Long, val durationS: Long,
                       val serverFiresAt: Long = firesAt) {
    /** What is said and shown when it rings. */
    val doneText get() = if (label.isBlank()) "Your timer is done" else "${label.replaceFirstChar { it.uppercase() }} timer is done"
    val title get() = label.ifBlank { "Timer" }
}

/**
 * Pure bookkeeping for client-scheduled timers: which timers are armed, and which
 * (id, server fires_at) pairs already finished (rang or were cancelled), so a server
 * that resends its active timers on reconnect cannot make a timer ring twice, while a
 * reused id with a new fire time still arms. Also queues cancels the user made on the
 * phone until they can be sent to the server. Persisted as one JSON string.
 *
 * Fire time: with `duration_s > 0` the phone rings at its own arrival time + duration
 * (the server clock may be off); `fires_at` is then only the resend identity.
 */
class TimerBook(private val clock: () -> Long = System::currentTimeMillis) {
    sealed class Change {
        data class Arm(val timer: ActiveTimer) : Change()
        /** A timer whose time already passed (missed while off): ring it now. */
        data class RingNow(val timer: ActiveTimer) : Change()
        data class Disarm(val id: String) : Change()
        object None : Change()
    }
    companion object {
        /** A timer missed by more than this (phone off, app killed) is dropped, not rung late. */
        const val LATE_LIMIT_MS = 60 * 60 * 1000L
        const val MAX_ACTIVE = 50
        const val MAX_FINISHED = 100
        const val MAX_OUTBOX = 50
        fun load(json: String?, clock: () -> Long = System::currentTimeMillis): TimerBook {
            val book = TimerBook(clock)
            if (json.isNullOrBlank()) return book
            try {
                val o = JSONObject(json)
                val a = o.optJSONArray("active") ?: JSONArray()
                for (i in 0 until a.length()) {
                    val t = a.optJSONObject(i) ?: continue
                    val id = t.optString("id").takeIf { it.isNotEmpty() } ?: continue
                    val firesAt = t.optLong("fires_at")
                    book.active[id] = ActiveTimer(id, t.optString("label"), firesAt, t.optLong("duration_s"),
                        t.optLong("server_fires_at", firesAt))
                }
                val f = o.optJSONArray("finished") ?: JSONArray()
                for (i in 0 until f.length()) {
                    val e = f.optJSONObject(i) ?: continue
                    val id = e.optString("id").takeIf { it.isNotEmpty() } ?: continue
                    book.finished.add(Finished(id, e.optLong("fires_at")))
                }
                val q = o.optJSONArray("outbox") ?: JSONArray()
                for (i in 0 until q.length()) q.optString(i).takeIf { it.isNotEmpty() }?.let { book.outbox.add(it) }
            } catch (_: Exception) { /* corrupt store: start empty rather than crash an alarm */ }
            return book
        }
    }

    private data class Finished(val id: String, val serverFiresAt: Long)
    private val active = linkedMapOf<String, ActiveTimer>()
    private val finished = LinkedHashSet<Finished>()
    private val outbox = LinkedHashSet<String>()

    /** Armed timers, soonest first. */
    fun timers(): List<ActiveTimer> = active.values.sortedBy { it.firesAt }
    fun get(id: String) = active[id]
    /** Whether [id] rang or was cancelled ([serverFiresAt] null: with any fire time). */
    fun isFinished(id: String, serverFiresAt: Long? = null) =
        finished.any { it.id == id && (serverFiresAt == null || it.serverFiresAt == serverFiresAt) }

    fun apply(e: TimerEvent): Change = when (e.action) {
        "cancel" -> cancel(e.id)
        else -> set(e)
    }

    private fun set(e: TimerEvent): Change {
        if (Finished(e.id, e.firesAt) in finished) return Change.None    // resent after it rang or was cancelled
        val old = active[e.id]
        // Resent while armed (reconnect): keep the phone's own fire time; only a label change is taken.
        if (old != null && old.serverFiresAt == e.firesAt && old.durationS == e.durationS) {
            if (old.label == e.label) return Change.None
            active[e.id] = old.copy(label = e.label)
            return Change.None
        }
        val now = clock()
        val t = ActiveTimer(e.id, e.label, if (e.durationS > 0) now + e.durationS * 1000 else e.firesAt, e.durationS, e.firesAt)
        if (t.firesAt < now - LATE_LIMIT_MS) { active.remove(t.id); finish(t); return Change.None }
        if (old == null && active.size >= MAX_ACTIVE) return Change.None
        active[t.id] = t
        return if (t.firesAt <= now) Change.RingNow(t) else Change.Arm(t)
    }

    /** Removes a timer (server cancel); remembered so a resend is ignored. */
    fun cancel(id: String): Change {
        val t = active.remove(id) ?: return Change.None
        finish(t)
        return Change.Disarm(id)
    }

    /** The user cancelled [id] on the phone: as [cancel], and queues a cancel for the server. */
    fun userCancel(id: String): Change {
        val c = cancel(id)
        if (c is Change.Disarm) {
            outbox.remove(id); outbox.add(id)
            while (outbox.size > MAX_OUTBOX) outbox.remove(outbox.first())
        }
        return c
    }

    /** Cancels waiting to be sent to the server, oldest first. */
    fun pendingCancels(): List<String> = outbox.toList()
    /** [id]'s cancel reached the server. */
    fun sent(id: String) { outbox.remove(id) }

    /** The alarm went off: returns the timer to ring, or null if it was cancelled meanwhile. */
    fun fired(id: String): ActiveTimer? {
        val t = active.remove(id) ?: return null
        finish(t)
        return t
    }

    /** After reboot/process death: what to arm again, what to ring late, what to drop. */
    fun rearm(): List<Change> {
        val now = clock()
        val out = mutableListOf<Change>()
        for (t in active.values.toList()) {
            when {
                t.firesAt < now - LATE_LIMIT_MS -> { active.remove(t.id); finish(t) }
                t.firesAt <= now -> out += Change.RingNow(t)
                else -> out += Change.Arm(t)
            }
        }
        return out
    }

    private fun finish(t: ActiveTimer) {
        val f = Finished(t.id, t.serverFiresAt)
        finished.remove(f); finished.add(f)
        while (finished.size > MAX_FINISHED) finished.remove(finished.first())
    }

    fun toJson(): String = JSONObject()
        .put("active", JSONArray().apply {
            for (t in active.values) put(JSONObject().put("id", t.id).put("label", t.label)
                .put("fires_at", t.firesAt).put("duration_s", t.durationS).put("server_fires_at", t.serverFiresAt))
        })
        .put("finished", JSONArray().apply { finished.forEach { put(JSONObject().put("id", it.id).put("fires_at", it.serverFiresAt)) } })
        .put("outbox", JSONArray().apply { outbox.forEach { put(it) } })
        .toString()
}

/** "4:05" / "1:02:03" until [firesAt]. */
fun timerRemaining(firesAt: Long, now: Long): String {
    val s = ((firesAt - now).coerceAtLeast(0) + 999) / 1000
    val h = s / 3600; val m = (s % 3600) / 60; val sec = s % 60
    return if (h > 0) "%d:%02d:%02d".format(h, m, sec) else "%d:%02d".format(m, sec)
}

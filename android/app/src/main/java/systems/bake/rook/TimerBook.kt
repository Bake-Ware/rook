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
            if (action == "set" && firesAt <= 0) return null
            return TimerEvent(action, id, label, firesAt, m.whole("duration_s") ?: 0L)
        }
    }
}

data class ActiveTimer(val id: String, val label: String, val firesAt: Long, val durationS: Long) {
    /** What is said and shown when it rings. */
    val doneText get() = if (label.isBlank()) "Your timer is done" else "${label.replaceFirstChar { it.uppercase() }} timer is done"
    val title get() = label.ifBlank { "Timer" }
}

/**
 * Pure bookkeeping for client-scheduled timers: which timers are armed, and which ids
 * already finished (rang or were cancelled), so a server that resends its active timers
 * on reconnect cannot make a timer ring twice. Persisted as one JSON string.
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
        fun load(json: String?, clock: () -> Long = System::currentTimeMillis): TimerBook {
            val book = TimerBook(clock)
            if (json.isNullOrBlank()) return book
            try {
                val o = JSONObject(json)
                val a = o.optJSONArray("active") ?: JSONArray()
                for (i in 0 until a.length()) {
                    val t = a.optJSONObject(i) ?: continue
                    val id = t.optString("id").takeIf { it.isNotEmpty() } ?: continue
                    book.active[id] = ActiveTimer(id, t.optString("label"), t.optLong("fires_at"), t.optLong("duration_s"))
                }
                val f = o.optJSONArray("finished") ?: JSONArray()
                for (i in 0 until f.length()) f.optString(i).takeIf { it.isNotEmpty() }?.let { book.finished.add(it) }
            } catch (_: Exception) { /* corrupt store: start empty rather than crash an alarm */ }
            return book
        }
    }

    private val active = linkedMapOf<String, ActiveTimer>()
    private val finished = LinkedHashSet<String>()

    /** Armed timers, soonest first. */
    fun timers(): List<ActiveTimer> = active.values.sortedBy { it.firesAt }
    fun get(id: String) = active[id]
    fun isFinished(id: String) = id in finished

    fun apply(e: TimerEvent): Change = when (e.action) {
        "cancel" -> cancel(e.id)
        else -> set(ActiveTimer(e.id, e.label, e.firesAt, e.durationS))
    }

    private fun set(t: ActiveTimer): Change {
        if (t.id in finished) return Change.None              // resent after it rang or was cancelled
        if (active[t.id] == t) return Change.None              // resent unchanged: already armed
        val now = clock()
        if (t.firesAt < now - LATE_LIMIT_MS) { finish(t.id); return Change.None }
        if (t.id !in active && active.size >= MAX_ACTIVE) return Change.None
        active[t.id] = t
        return if (t.firesAt <= now) Change.RingNow(t) else Change.Arm(t)
    }

    /** Removes a timer (server cancel or the user tapping cancel); remembered so a resend is ignored. */
    fun cancel(id: String): Change {
        val had = active.remove(id) != null
        finish(id)
        return if (had) Change.Disarm(id) else Change.None
    }

    /** The alarm went off: returns the timer to ring, or null if it was cancelled meanwhile. */
    fun fired(id: String): ActiveTimer? {
        val t = active.remove(id) ?: return null
        finish(id)
        return t
    }

    /** After reboot/process death: what to arm again, what to ring late, what to drop. */
    fun rearm(): List<Change> {
        val now = clock()
        val out = mutableListOf<Change>()
        for (t in active.values.toList()) {
            when {
                t.firesAt < now - LATE_LIMIT_MS -> { active.remove(t.id); finish(t.id) }
                t.firesAt <= now -> out += Change.RingNow(t)
                else -> out += Change.Arm(t)
            }
        }
        return out
    }

    private fun finish(id: String) {
        finished.remove(id); finished.add(id)
        while (finished.size > MAX_FINISHED) finished.remove(finished.first())
    }

    fun toJson(): String = JSONObject()
        .put("active", JSONArray().apply {
            for (t in active.values) put(JSONObject().put("id", t.id).put("label", t.label)
                .put("fires_at", t.firesAt).put("duration_s", t.durationS))
        })
        .put("finished", JSONArray().apply { finished.forEach { put(it) } })
        .toString()
}

/** "4:05" / "1:02:03" until [firesAt]. */
fun timerRemaining(firesAt: Long, now: Long): String {
    val s = ((firesAt - now).coerceAtLeast(0) + 999) / 1000
    val h = s / 3600; val m = (s % 3600) / 60; val sec = s % 60
    return if (h > 0) "%d:%02d:%02d".format(h, m, sec) else "%d:%02d".format(m, sec)
}

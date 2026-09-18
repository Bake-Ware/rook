package systems.bake.rook

import org.json.JSONObject

/** Optional display metadata; local monotonic time, never server clock, drives stalls. */
data class ActivityEvent(val turn: Int, val seq: Long, val ts: Long, val phase: String,
    val label: String, val detail: String?, val tool: String?, val worker: String?, val cap: String?,
    val elapsedMs: Long?, val timeoutMs: Long?, val status: String?) {
    val failed get() = phase == "error" || status == "failed" || status == "error"
    val icon get() = when { failed -> "!"; phase == "done" || phase == "tool_result" -> "✓"
        phase == "speaking" -> "♪"; phase == "tool_wait" -> "◷"; else -> "•" }
    companion object {
        private val phases = setOf("heard", "planning", "planned", "retry", "fallback", "tool_start", "tool_wait", "tool_result", "speaking", "done", "error")
        private fun JSONObject.integer(key: String): Long? = (opt(key) as? Number)?.toDouble()
            ?.takeIf { it.isFinite() && it >= 0 && it < Long.MAX_VALUE.toDouble() && it % 1.0 == 0.0 }?.toLong()
        private fun JSONObject.text(key: String) = (opt(key) as? String)?.takeIf { it.isNotBlank() }
        private fun JSONObject.duration(key: String) = (opt(key) as? Number)?.toDouble()?.takeIf { it.isFinite() && it >= 0 && it < Long.MAX_VALUE.toDouble() }?.toLong()
        fun parse(m: JSONObject): ActivityEvent? {
            if (m.optString("type") != "activity") return null
            val turn = m.integer("turn")?.takeIf { it <= Int.MAX_VALUE }?.toInt() ?: return null
            val seq = m.integer("seq") ?: return null
            val ts = m.integer("ts") ?: return null
            val phase = m.optString("phase").takeIf { it in phases } ?: return null
            return ActivityEvent(turn, seq, ts, phase, m.text("label") ?: phase.replace('_', ' '),
                m.text("detail"), m.text("tool"), m.text("worker"), m.text("cap"), m.duration("elapsed_ms"), m.duration("timeout_ms"), m.text("status"))
        }
    }
}

data class TurnKey(val connection: Long, val turn: Int)
class ActivityTimeline {
    val turns = linkedMapOf<TurnKey, MutableList<ActivityEvent>>()
    private var connection = -1L
    private var lastSeq = -1L
    fun local(connection: Long, event: ActivityEvent) { turns.getOrPut(TurnKey(connection, event.turn)) { mutableListOf() }.add(event) }
    fun add(connection: Long, event: ActivityEvent): Boolean {
        if (this.connection != connection) { this.connection = connection; lastSeq = -1 }
        if (event.seq <= lastSeq) return false
        lastSeq = event.seq
        turns.getOrPut(TurnKey(connection, event.turn)) { mutableListOf() }.add(event)
        return true
    }
}

class UnseenItems {
    var count = 0; private set
    var failed = false; private set
    fun add(isFailure: Boolean, visible: Boolean) { if (!visible) { count++; failed = failed || isFailure } }
    fun seen() { count = 0; failed = false }
}

class TurnStatus {
    data class Display(val text: String, val seconds: Long, val severity: Int = 0)
    private var activitySupported = false
    private var turn = -1
    private var active = false
    private var started = 0L
    private var lastActivity = 0L
    private var label = "Listening"
    private var waitingSince: Long? = null
    private var waitingWorker = "worker"
    fun reset() { activitySupported = false; turn = -1; active = false; waitingSince = null }
    fun begin(now: Long, text: String) { if (!active) started = now; active = true; label = text; waitingSince = null; lastActivity = now }
    fun finishLegacy() { if (!activitySupported) finish() }
    fun finish() { active = false; waitingSince = null }
    fun legacyState(state: String, now: Long) {
        if (state in listOf("idle", "standby")) finish()
        if (state == "reconnecting" && active) { label = "Retrying"; waitingSince = null }
        if (activitySupported) {
            if (!active && state == "thinking") begin(now, "Planning")
            return
        }
        when (state) {
            "thinking" -> begin(now, "Planning")
            "speaking" -> begin(now, "Speaking")
            "reconnecting" -> if (active) { label = "Retrying" }
            "listening" -> if (active && label != "Listening") finish()
        }
    }
    fun event(e: ActivityEvent, now: Long) {
        activitySupported = true
        if (e.turn < turn) return
        if (e.turn > turn) { turn = e.turn; started = now; active = true }
        lastActivity = now
        if (e.phase == "done") { finish(); return }
        // Late metadata from a completed turn must not resurrect its strip.
        if (!active) return
        waitingSince = if (e.phase == "tool_wait") now - (e.elapsedMs ?: 0) else null
        waitingWorker = e.worker ?: e.tool ?: "worker"
        label = when (e.phase) {
            "heard" -> "Heard you"
            "planning", "planned" -> "Planning"
            "tool_start" -> "Running " + listOfNotNull(e.worker, e.cap ?: e.tool).joinToString(" ").ifBlank { "tool" }
            "tool_wait" -> "Waiting on $waitingWorker"
            "speaking", "fallback" -> "Speaking"
            "retry" -> "Retrying"
            "error" -> "Error"
            "tool_result" -> if (e.failed) "Tool failed" else "Planning"
            else -> e.label
        }
    }
    fun display(now: Long): Display? {
        if (!active) return null
        val elapsed = ((now - started).coerceAtLeast(0)) / 1000
        val silent = now - lastActivity
        if (activitySupported && silent >= 45000) return Display("Stalled", elapsed, 2)
        if (activitySupported && silent >= 15000) return Display("No response - may be stalled", elapsed, 1)
        val text = waitingSince?.let { "Waiting on $waitingWorker - ${(now - it).coerceAtLeast(0) / 1000}s" } ?: label
        return Display(text, elapsed)
    }
}

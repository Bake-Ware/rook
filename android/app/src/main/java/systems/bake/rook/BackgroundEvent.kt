package systems.bake.rook

import org.json.JSONObject

/**
 * One Background worker step (`background` event, docs/design/voice-front-background.md).
 * Display only: never used to route or gate a turn.
 */
data class BackgroundEvent(
    val turn: Int, val seq: Long, val ts: Long, val kind: String, val text: String,
    val tool: String?, val args: String?, val result: String?, val status: String?, val elapsedMs: Long?,
) {
    val failed get() = kind == "error" || status == "failed"
    val icon get() = when {
        failed -> "!"
        status == "cancelled" -> "⊘"
        else -> when (kind) {
            "start" -> "▶"; "prefetch" -> "↓"; "thought" -> "…"; "tool_call" -> "⚙"
            "tool_result" -> "✓"; "board" -> "▤"; "followup" -> "↩"; "dropped" -> "⊘"
            "done" -> "■"; else -> "•"
        }
    }

    companion object {
        val kinds = setOf("start", "prefetch", "thought", "tool_call", "tool_result", "board", "followup", "dropped", "done", "error")
        const val MAX_RESULT = 2000
        private fun JSONObject.whole(key: String): Long? = (opt(key) as? Number)?.toDouble()
            ?.takeIf { it.isFinite() && it >= 0 && it < Long.MAX_VALUE.toDouble() }?.toLong()
        private fun JSONObject.text(key: String) = (opt(key) as? String)?.takeIf { it.isNotBlank() }

        fun parse(m: JSONObject): BackgroundEvent? {
            if (m.optString("type") != "background") return null
            val turn = (m.opt("turn") as? Number)?.toDouble()
                ?.takeIf { it.isFinite() && it >= 0 && it <= Int.MAX_VALUE && it % 1.0 == 0.0 }?.toInt() ?: return null
            val seq = m.whole("seq") ?: return null
            val kind = m.optString("kind").takeIf { it in kinds } ?: return null
            val args = when (val a = m.opt("args")) {
                is JSONObject -> if (a.length() == 0) null else a.toString(2)
                is String -> a.takeIf { it.isNotBlank() }
                else -> null
            }
            return BackgroundEvent(turn, seq, m.whole("ts") ?: System.currentTimeMillis(), kind,
                m.text("text") ?: kind.replace('_', ' '), m.text("tool"), args,
                m.text("result")?.take(MAX_RESULT), m.text("status"), m.whole("elapsed_ms"))
        }
    }
}

/**
 * Background steps grouped by turn; ignores replays within a connection; keeps the newest
 * [maxTurns] turns and the newest [maxSteps] steps of each turn (older ones counted in [truncated]).
 * Main thread only.
 */
class BackgroundTimeline(private val maxTurns: Int = 40, private val maxSteps: Int = 200) {
    val turns = linkedMapOf<TurnKey, ArrayDeque<BackgroundEvent>>()
    /** Steps dropped from the start of a turn because it exceeded [maxSteps]. */
    val truncated = mutableMapOf<TurnKey, Int>()
    /** Count of steps ever accepted, so a screen can tell how many it has not seen. */
    var version = 0L; private set
    /** [version] at the newest failed step (0 if none). */
    var lastFailure = 0L; private set
    private var connection = -1L
    private var lastSeq = -1L
    fun add(connection: Long, event: BackgroundEvent): Boolean {
        if (this.connection != connection) { this.connection = connection; lastSeq = -1 }
        if (event.seq <= lastSeq) return false
        lastSeq = event.seq
        val key = TurnKey(connection, event.turn)
        val steps = turns.getOrPut(key) { ArrayDeque() }
        steps.addLast(event)
        if (steps.size > maxSteps) { steps.removeFirst(); truncated[key] = (truncated[key] ?: 0) + 1 }
        while (turns.size > maxTurns) truncated.remove(turns.keys.first().also { turns.remove(it) })
        version++
        if (event.failed) lastFailure = version
        return true
    }
    /** A turn is still running until Background reports done, error or dropped. */
    fun running(key: TurnKey) = turns[key]?.none { it.kind in setOf("done", "error", "dropped") } ?: false
}

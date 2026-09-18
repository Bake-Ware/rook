package systems.bake.rook

import org.json.JSONObject
import kotlin.math.roundToInt

/** Display-only protocol metadata: never used to route or gate a turn. */
data class Decision(
    val turn: Int, val source: String, val mode: String, val status: String,
    val latencyMs: Double?, val model: String?, val adapter: String?, val calibration: String?,
    val answers: List<Answer>, val error: String?,
    val engineStatus: String = status,
) {
    data class Answer(val id: String, val type: String, val p: Double?, val choice: String?,
                      val probabilities: Map<String, Double>, val confidence: Double?,
                      val level: Double?, val expected: Double?)

    val engineLabel get() = when (engineStatus) {
        "ok" -> "ok"; "disabled" -> "engine off"; "skipped" -> "skipped"
        "timeout" -> "timed out"; "error" -> "error"; else -> engineStatus.ifBlank { "unknown" }
    }
    val failed get() = engineStatus == "error" || engineStatus == "timeout"

    fun summary(): String {
        val intent = answers.find { it.id == "intent" }
        val confirm = answers.find { it.id == "needs_confirmation" }?.p
        return listOfNotNull(
            intent?.choice?.let { choice -> choice + (intent.probabilities[choice]?.let { " ${percent(it)}" } ?: "") }
                ?: status,
            confirm?.let { "confirm ${if (it >= 0.5) "yes" else "no"}" },
            latencyMs?.let { "${it.roundToInt()} ms" }
        ).joinToString(" · ", prefix = "thinking: ")
    }

    fun details(): String = buildString {
        append("status: $status · $mode · $source")
        append("\nmodel: ${model ?: "—"}\nadapter: ${adapter ?: "—"}\ncalibration: ${calibration ?: "—"}")
        error?.let { append("\nerror: $it") }
        for (a in answers) {
            append("\n${a.id}: ")
            append(listOfNotNull(a.p?.let { "p=${percent(it)}" }, a.choice,
                a.level?.let { "level=$it" }, a.expected?.let { "expected=$it" }).joinToString(" · "))
            a.probabilities.forEach { (key, value) -> append("\n  $key: ${percent(value)}") }
            a.confidence?.let { append("\n  confidence: ${percent(it)}") }
        }
    }

    companion object {
        private val known = mapOf("needs_response" to "noul", "intent" to "choice",
            "needs_confirmation" to "noul", "context_source" to "choice", "urgency" to "score", "is_correction" to "noul")
        private fun percent(value: Double) = "${(value * 100).roundToInt()}%"
        private fun JSONObject.number(key: String): Double? = (opt(key) as? Number)?.toDouble()?.takeIf { it.isFinite() }
        private fun JSONObject.probability(key: String) = number(key)?.takeIf { it in 0.0..1.0 }
        private fun JSONObject.string(key: String) = (opt(key) as? String)?.takeIf { it.isNotEmpty() }
        fun parse(m: JSONObject): Decision? {
            if (m.optString("type") != "decision") return null
            val turn = m.number("turn")?.takeIf { it >= 0 && it <= Int.MAX_VALUE && it % 1.0 == 0.0 }?.toInt() ?: return null
            val engineStatus = m.string("engine_status") ?: m.string("status") ?: "ok"
            val answers = mutableListOf<Answer>()
            val array = if (engineStatus == "ok") m.optJSONArray("answers") else null
            for (i in 0 until (array?.length() ?: 0)) {
                val a = array?.optJSONObject(i) ?: continue
                val id = a.optString("id")
                val type = known[id] ?: continue
                if (a.optString("type") != type) continue
                val probabilities = linkedMapOf<String, Double>()
                a.optJSONObject("probabilities")?.let { ps ->
                    ps.keys().forEach { key -> ps.probability(key)?.let { probabilities[key] = it } }
                }
                answers += Answer(id, type, a.probability("p"), a.string("choice"), probabilities,
                    a.probability("confidence"), a.number("level"), a.number("expected"))
            }
            val engine = m.optJSONObject("engine")
            return Decision(turn, m.optString("source"), m.optString("mode"), m.optString("status"),
                (m.number("elapsed_ms") ?: m.number("latency_ms"))?.takeIf { it >= 0 }, m.string("model") ?: engine?.string("model"), m.string("adapter") ?: engine?.string("adapter"),
                engine?.string("calibration"), answers, m.string("detail") ?: m.string("reason") ?: m.string("error"), engineStatus)
        }
    }
}

/** Main-thread attachment index. Connection boundaries prevent reused turn IDs matching old UI. */
class DecisionAttachments<T>(private val attach: (T, Decision) -> Unit) {
    private val decisions = linkedMapOf<Int, Decision>()
    private val assistants = mutableMapOf<Int, T>()
    private val utterances = mutableMapOf<Int, T>()
    fun message(turn: Int, message: T, voice: Boolean = false) {
        (if (voice) utterances else assistants)[turn] = message
        deliver(turn)
    }
    fun decision(decision: Decision) {
        decisions[decision.turn] = decision
        // Bound unmatched metadata when the UI missed messages while paused.
        if (decisions.size > 256) decisions.remove(decisions.keys.first())
        deliver(decision.turn)
    }
    private fun deliver(turn: Int) {
        val decision = decisions[turn] ?: return
        val target = if (decision.source == "voice") utterances[turn] else assistants[turn]
        target?.let { attach(it, decision) }
    }
    fun clear() { decisions.clear(); assistants.clear(); utterances.clear() }
}

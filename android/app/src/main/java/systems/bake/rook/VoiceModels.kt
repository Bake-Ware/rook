package systems.bake.rook

import org.json.JSONObject

/**
 * The house agent's models as the hub reports them (home.voice_models through the
 * voice server): the voice (front), the agent (back), their efforts, and the choices
 * with a one-line note each. Changing them is the owner's call; the hub applies it live.
 */
object VoiceModels {
    const val PREF = "voice_models_json"

    data class Choice(val id: String, val label: String, val note: String)
    data class State(
        val front: String, val frontEffort: String, val back: String, val backEffort: String,
        val fronts: List<Choice>, val backs: List<Choice>, val efforts: List<String>,
        val error: String?,
    )

    fun parse(raw: String?): State? {
        if (raw.isNullOrBlank()) return null
        val m = try { JSONObject(raw) } catch (_: Exception) { return null }
        val choices = m.optJSONObject("choices") ?: return null
        fun list(key: String): List<Choice> {
            val a = choices.optJSONArray(key) ?: return emptyList()
            return (0 until a.length()).mapNotNull { i ->
                a.optJSONObject(i)?.let { Choice(it.optString("id"), it.optString("label", it.optString("id")), it.optString("note")) }
                    ?.takeIf { it.id.isNotBlank() }
            }
        }
        val efforts = choices.optJSONArray("efforts")?.let { a -> (0 until a.length()).map { a.optString(it) } } ?: emptyList()
        val front = m.optJSONObject("front") ?: JSONObject()
        val back = m.optJSONObject("back") ?: JSONObject()
        return State(front.optString("model"), front.optString("effort"), back.optString("model"),
            back.optString("effort"), list("front"), list("back"), efforts,
            m.optString("error").takeIf { m.has("ok") && !m.optBoolean("ok", true) && it.isNotBlank() })
    }

    fun label(choices: List<Choice>, id: String): String = choices.firstOrNull { it.id == id }?.label ?: id
    fun note(choices: List<Choice>, id: String): String = choices.firstOrNull { it.id == id }?.note ?: ""
    fun effortLabel(e: String): String = if (e.isBlank()) "default" else e

    /** The args of a models_set: only what changed, so an untouched picker is left alone. */
    fun changes(now: State, front: String, frontEffort: String, back: String, backEffort: String): JSONObject {
        val out = JSONObject()
        if (front != now.front) out.put("front", front)
        if (frontEffort != now.frontEffort) out.put("front_effort", frontEffort)
        if (back != now.back) out.put("back", back)
        if (backEffort != now.backEffort) out.put("back_effort", backEffort)
        return out
    }
}

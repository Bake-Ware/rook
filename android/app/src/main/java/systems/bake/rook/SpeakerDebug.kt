package systems.bake.rook

import org.json.JSONObject
import java.util.Locale

/**
 * Settings > Debug: show speaker ID. With it on, the voice server sends a "speaker" event
 * for each utterance and the chat shows one line: who the voice took the speaker for, the
 * score against the threshold, what speaker ID heard, and whether the clip was learned.
 * ("hey Bake, what's up" got "I don't know you" with no way to see why, 2026-10-27.)
 */
object SpeakerDebug {
    const val PREF = "debug_speaker"

    private fun num(m: JSONObject, key: String): Double? =
        if (m.has(key) && !m.isNull(key)) m.optDouble(key).takeUnless { it.isNaN() } else null

    private fun text(m: JSONObject, key: String): String? =
        if (m.has(key) && !m.isNull(key)) m.optString(key).takeIf { it.isNotBlank() } else null

    private fun fmt(v: Double) = String.format(Locale.US, "%.2f", v)

    /** The chat line for one "speaker" event, or null when it says nothing. */
    fun line(m: JSONObject): String? {
        val label = text(m, "label") ?: return null
        val parts = mutableListOf("speaker: " + (if (label == "guest") "someone else" else label))
        val score = num(m, "score")
        val threshold = num(m, "threshold")
        if (score != null) parts += "score ${fmt(score)}" + (threshold?.let { " / ${fmt(it)}" } ?: "")
        text(m, "heard")?.takeIf { it != label }?.let { parts += "heard as " + (if (it == "guest") "unknown" else it) }
        if (text(m, "basis") == "owner phone") parts += "yours by default (your phone)"
        num(m, "norm")?.let { parts += "norm ${fmt(it)}" }
        m.optJSONObject("clip")?.let { clip -> text(clip, "setting")?.let { parts += "closest clip: $it" } }
        when {
            num(m, "learned") != null -> parts += "learned (clip ${m.optLong("learned")})"
            text(m, "skipped") != null -> parts += "not learned: " + text(m, "skipped")
        }
        text(m, "dropped")?.let { parts += "ignored: $it" }
        return parts.joinToString(" · ")
    }
}

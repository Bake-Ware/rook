package systems.bake.rook.wearlink

import org.json.JSONArray
import org.json.JSONObject

/**
 * The conversation the watch shows, by the phone app's rules: a heard line is the person's;
 * reply pieces join one Rook line until the reply is done; a note (an error) stands alone.
 * Immutable, newest last, at most [capacity] lines. The phone sends its recent lines as
 * [WearLink.HISTORY] when a watch conversation starts ([toJson] / [fromJson]).
 */
data class Transcript(
    val lines: List<Line> = emptyList(),
    /** The last line is a Rook reply still coming in. */
    val open: Boolean = false,
    val capacity: Int = 40,
) {
    enum class Who { USER, ROOK, NOTE }

    data class Line(val who: Who, val text: String)

    fun heard(text: String): Transcript = if (text.isBlank()) this else add(Line(Who.USER, text.trim()))

    fun reply(piece: String): Transcript {
        if (piece.isBlank()) return this
        val last = lines.lastOrNull()
        if (!open || last == null || last.who != Who.ROOK) return add(Line(Who.ROOK, piece.trim())).copy(open = true)
        return copy(lines = lines.dropLast(1) + last.copy(text = last.text + " " + piece.trim()))
    }

    fun done(): Transcript = if (open) copy(open = false) else this

    fun note(text: String): Transcript = if (text.isBlank()) this else add(Line(Who.NOTE, text.trim()))

    private fun add(line: Line) = copy(lines = (lines + line).takeLast(capacity), open = false)

    /**
     * The newest lines that fit in one link message, oldest first; each line at most
     * [MAX_LINE] characters (a long reply keeps its end, where it was going).
     */
    fun toJson(maxBytes: Int = WearLink.MAX_PAYLOAD / 2): JSONObject {
        val out = ArrayList<JSONObject>()
        var size = 16
        for (line in lines.asReversed()) {
            if (line.who == Who.NOTE) continue
            val text = if (line.text.length > MAX_LINE) "…" + line.text.takeLast(MAX_LINE - 1) else line.text
            val o = JSONObject().put("who", if (line.who == Who.USER) "user" else "rook").put("text", text)
            size += o.toString().toByteArray(Charsets.UTF_8).size + 1
            if (size > maxBytes) break
            out += o
        }
        return JSONObject().put("lines", JSONArray().also { a -> out.asReversed().forEach { a.put(it) } })
    }

    companion object {
        const val MAX_LINE = 1500

        fun of(lines: List<Line>, capacity: Int = 40) = Transcript(lines.takeLast(capacity), false, capacity)

        fun fromJson(json: JSONObject, capacity: Int = 40): Transcript {
            val a = json.optJSONArray("lines") ?: return Transcript(capacity = capacity)
            val lines = (0 until a.length()).mapNotNull { i ->
                val o = a.optJSONObject(i) ?: return@mapNotNull null
                val text = o.optString("text").trim()
                if (text.isEmpty()) null else Line(if (o.optString("who") == "user") Who.USER else Who.ROOK, text)
            }
            return of(lines, capacity)
        }
    }
}

package systems.bake.rook

import org.json.JSONArray
import org.json.JSONObject

/**
 * One node of a screen snapshot: what the accessibility tree said, copied once
 * so the summary, the screen revision and field lookup are plain functions
 * (unit-tested off-device). [ref] is the live AccessibilityNodeInfo to act on.
 */
class SNode(
    val text: String? = null,
    val desc: String? = null,
    val id: String? = null,
    val cls: String? = null,
    val hint: String? = null,
    /** Text of the label view that names this one (labelFor / labeledBy). */
    val labeledBy: String? = null,
    val clickable: Boolean = false,
    val longClickable: Boolean = false,
    val scrollable: Boolean = false,
    val editable: Boolean = false,
    val checkable: Boolean = false,
    val checked: Boolean = false,
    val selected: Boolean = false,
    val focused: Boolean = false,
    val password: Boolean = false,
    /** The text shown is the hint (an empty field), API 26+; else text == hint. */
    val showingHint: Boolean = false,
    val enabled: Boolean = true,
    val visible: Boolean = true,
    /** left, top, right, bottom in screen pixels. */
    val bounds: IntArray = IntArray(4),
    val children: List<SNode> = emptyList(),
    val ref: Any? = null,
) {
    val actionable: Boolean get() = clickable || longClickable || editable || checkable || scrollable

    /** Own label: text, else content description (a field's typed text is its value, not its label). */
    fun ownLabel(): String {
        if (!editable) {
            val t = text?.trim()
            if (!t.isNullOrEmpty()) return t
        }
        return desc?.trim() ?: ""
    }

    /** What a field holds now: empty when it only shows its hint; never a password. */
    fun value(): String? {
        if (!editable || password) return null
        val t = text ?: return null
        if (showingHint || (hint != null && t == hint)) return ""
        return t
    }
}

/** An accessibility window: its type (AccessibilityWindowInfo.TYPE_*), z-layer and whether it is active. */
class SWindow(val type: Int, val layer: Int, val active: Boolean)

object ScreenModel {
    /** AccessibilityWindowInfo.TYPE_APPLICATION (activities, dialogs, popups and sub-windows). */
    const val TYPE_APPLICATION = 1

    const val LABEL_MAX = 80
    const val DETAIL_MAX = 120
    const val TEXT_MAX = 160

    /**
     * Which windows the ui.* caps read, in order: the active window first (so
     * indices into it stay what they were), then application windows above it,
     * lowest first. A dropdown or popup menu is a separate, non-focusable window
     * on top of the activity; reading only the active window missed it, and
     * agents fell back to tapping pixels. The keyboard, the status and
     * navigation bars and accessibility overlays are left out.
     */
    fun pickWindows(wins: List<SWindow>): List<Int> {
        val active = wins.indexOfFirst { it.active }
        if (active < 0) return emptyList()
        val floor = wins[active].layer
        val above = wins.indices.filter { i ->
            i != active && wins[i].type == TYPE_APPLICATION && wins[i].layer > floor
        }.sortedBy { wins[it].layer }
        return listOf(active) + above
    }

    private fun cut(s: String, max: Int): String {
        val one = s.replace(Regex("\\s+"), " ").trim()
        return if (one.length <= max) one else one.substring(0, max - 1) + "…"
    }

    private fun shortId(id: String?): String? = id?.substringAfter(":id/")?.takeIf { it.isNotBlank() }

    fun kind(n: SNode): String = when {
        n.editable -> "field"
        n.checkable -> "toggle"
        n.scrollable -> "list"
        else -> "button"
    }

    /** Labels of non-actionable visible descendants (an unlabeled row's TextViews), in order. */
    private fun innerLabels(n: SNode, out: MutableList<SNode>, depth: Int = 0) {
        if (depth > 30 || out.size >= 6) return
        for (c in n.children) {
            if (c.actionable && c.visible) continue
            if (c.visible && c.ownLabel().isNotEmpty()) out.add(c)
            innerLabels(c, out, depth + 1)
        }
    }

    private fun center(n: SNode) = JSONArray(listOf((n.bounds[0] + n.bounds[2]) / 2, (n.bounds[1] + n.bounds[3]) / 2))

    /**
     * A compact screen: [items] are the things to act on (label, kind, short id,
     * center), [text] the other text top to bottom, [rev] a revision that
     * changes when what is shown changes. A clickable row with no label of its
     * own takes its inner texts as label and detail, so they are not repeated.
     */
    fun summary(roots: List<SNode>, maxItems: Int = 60, maxText: Int = 40): JSONObject {
        val items = JSONArray()
        val text = JSONArray()
        val used = HashSet<SNode>()
        var moreItems = 0
        var moreText = 0
        var focus: String? = null
        var lastText: String? = null
        fun walk(n: SNode, depth: Int) {
            if (depth > 60) return
            if (n.visible && n.actionable) {
                val inner = ArrayList<SNode>()
                if (!n.scrollable) innerLabels(n, inner)
                val own = n.ownLabel()
                val labels = inner.map { it.ownLabel() }.distinct()
                val label = when {
                    own.isNotEmpty() -> own
                    n.editable -> n.labeledBy ?: n.hint ?: ""
                    labels.isNotEmpty() -> labels[0]
                    n.scrollable -> shortId(n.id) ?: ""
                    else -> ""
                }
                val detail = labels.filter { it != label }.take(2)
                used.addAll(inner)
                val id = shortId(n.id)
                if (label.isNotEmpty() || id != null || n.editable) {
                    if (items.length() < maxItems) {
                        items.put(JSONObject().apply {
                            put("label", cut(label, LABEL_MAX))
                            put("kind", kind(n))
                            if (detail.isNotEmpty()) put("detail", cut(detail.joinToString(" · "), DETAIL_MAX))
                            id?.let { put("id", it) }
                            if (n.checkable) put("checked", n.checked)
                            if (n.selected) put("selected", true)
                            if (!n.enabled) put("enabled", false)
                            if (n.editable) {
                                n.value()?.let { put("value", cut(it, TEXT_MAX)) }
                                if (n.password) put("password", true)
                                n.hint?.takeIf { it.isNotBlank() && it != label }?.let { put("hint", cut(it, LABEL_MAX)) }
                            }
                            if (n.focused && n.editable) put("focused", true)
                            put("at", center(n))
                        })
                    } else moreItems++
                }
                if (n.focused && n.editable) focus = label.ifEmpty { id ?: "field" }
            } else if (n.visible && n !in used) {
                val l = n.ownLabel()
                // A labelled container and its own text say the same thing once.
                if (l.isNotEmpty() && l != lastText) {
                    if (text.length() < maxText) text.put(cut(l, TEXT_MAX)) else moreText++
                    lastText = l
                }
            }
            for (c in n.children) walk(c, depth + 1)
        }
        roots.forEach { walk(it, 0) }
        return JSONObject().apply {
            focus?.let { put("focus", it) }
            put("items", items)
            put("text", text)
            if (moreItems > 0) put("more_items", moreItems)
            if (moreText > 0) put("more_text", moreText)
            put("rev", rev(roots))
        }
    }

    /**
     * A short revision of what is shown: visible labels, field values, toggle
     * states and the number of windows. Positions are left out, so an
     * animation that only moves things does not count as a change.
     */
    fun rev(roots: List<SNode>): String {
        var h = -0x340d631b7bdddcdbL   // FNV-1a 64 offset basis
        fun mix(s: String) {
            for (ch in s) { h = h xor ch.code.toLong(); h *= 0x100000001b3L }
            h = h xor 0x1fL; h *= 0x100000001b3L
        }
        fun walk(n: SNode, depth: Int) {
            if (depth > 60) return
            if (n.visible) {
                val l = n.ownLabel()
                if (l.isNotEmpty()) mix(l)
                n.value()?.let { mix("=$it") }
                if (n.checkable) mix(if (n.checked) "[x]" else "[ ]")
                if (n.selected) mix("[sel]")
            }
            for (c in n.children) walk(c, depth + 1)
        }
        mix("w${roots.size}")
        roots.forEach { walk(it, 0) }
        return java.lang.Long.toHexString(h).padStart(16, '0').substring(4)
    }

    private fun norm(s: String?) = s?.trim()?.lowercase() ?: ""

    private fun same(have: String?, want: String, contains: Boolean): Boolean {
        val h = norm(have)
        if (h.isEmpty()) return false
        val w = norm(want)
        return if (contains) h.contains(w) else h == w
    }

    private fun idSame(have: String?, want: String, contains: Boolean): Boolean {
        val full = norm(have)
        if (full.isEmpty()) return false
        val w = norm(want)
        return if (contains) full.contains(w) else full == w || full.substringAfter(":id/") == w
    }

    fun flatten(roots: List<SNode>): List<SNode> {
        val out = ArrayList<SNode>()
        fun walk(n: SNode, depth: Int) {
            if (depth > 60 || out.size > 4000) return
            out.add(n)
            for (c in n.children) walk(c, depth + 1)
        }
        roots.forEach { walk(it, 0) }
        return out
    }

    /** Visible editable fields, top to bottom. */
    fun fields(roots: List<SNode>): List<SNode> = flatten(roots).filter { it.visible && it.editable }

    /** How a field is named in a not-found reply: label, hint or id. */
    fun fieldName(n: SNode): String =
        listOf(n.labeledBy, n.desc, n.hint, shortId(n.id)).firstOrNull { !it.isNullOrBlank() }?.trim() ?: "field"

    /**
     * Fields named by [label] (its hint, content description, labelFor view,
     * current text, or a text just above it or to its left on the same row)
     * and/or [id]. With neither: the focused field, else the only one on screen.
     */
    fun findFields(roots: List<SNode>, label: String?, id: String?, contains: Boolean): List<SNode> {
        val all = fields(roots)
        if (label == null && id == null) {
            all.firstOrNull { it.focused }?.let { return listOf(it) }
            return if (all.size == 1) all else emptyList()
        }
        val direct = all.filter { f ->
            (id == null || idSame(f.id, id, contains)) &&
                (label == null || same(f.hint, label, contains) || same(f.desc, label, contains) ||
                    same(f.labeledBy, label, contains) || (!f.password && same(f.value(), label, contains)))
        }
        if (direct.isNotEmpty() || label == null) return direct
        // A caption next to the field: the nearest field below it (within
        // 400 px, overlapping horizontally) or to its right on the same row.
        val out = LinkedHashSet<SNode>()
        for (t in flatten(roots)) {
            if (!t.visible || t.editable || !same(t.ownLabel(), label, contains)) continue
            val b = t.bounds
            val near = all.filter { f ->
                val fb = f.bounds
                val below = fb[1] >= b[1] && fb[1] - b[3] <= 400 && fb[0] < b[2] && fb[2] > b[0]
                val right = fb[0] >= b[2] - 4 && fb[1] < b[3] && fb[3] > b[1]
                (id == null || idSame(f.id, id, contains)) && (below || right)
            }.minByOrNull { f -> Math.abs(f.bounds[1] - b[1]) + Math.abs(f.bounds[0] - b[0]) }
            near?.let { out.add(it) }
        }
        return out.toList()
    }
}

package systems.bake.rook.core.worker

import org.json.JSONArray
import org.json.JSONObject

/**
 * One node of what is on screen, copied once from the accessibility tree (brief AP5), so reading,
 * matching and choosing are plain functions that run (and are tested) off the phone. [ref] is the
 * live AccessibilityNodeInfo that an action is done on; [parent] is set by [Screen.link].
 *
 * The model is 0.5's (origin/castling android/app ScreenModel.kt): the same summary, revision and
 * field rules, with none of its cuts. 0.5 cut labels at 80 characters, details at 120, text lines at
 * 160, a row's inner labels at 6, a tree at depth 60 and 4000 nodes, and near matches at 10 or 15;
 * here everything on screen is read and named in full.
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
    var parent: SNode? = null
    var depth: Int = 0

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

    fun centerX() = (bounds[0] + bounds[2]) / 2
    fun centerY() = (bounds[1] + bounds[3]) / 2
}

/** An accessibility window: its type (AccessibilityWindowInfo.TYPE_*), z-layer, whether it is active, and its app. */
class SWindow(val type: Int, val layer: Int, val active: Boolean, val pkg: String? = null)

object Screen {
    /** AccessibilityWindowInfo.TYPE_APPLICATION (activities, dialogs, popups and sub-windows). */
    const val TYPE_APPLICATION = 1

    /** Sets each node's parent and depth, so a click finds the nearest clickable parent. */
    fun link(roots: List<SNode>): List<SNode> {
        fun walk(n: SNode, depth: Int) {
            n.depth = depth
            for (c in n.children) { c.parent = n; walk(c, depth + 1) }
        }
        roots.forEach { walk(it, 0) }
        return roots
    }

    /**
     * Which windows the ui caps read, top first: the same app's windows above the active one (highest
     * first), then the active window (0.5's rule). A dropdown or popup menu is a window of its own on top
     * of the activity; another app's window, the keyboard and the system bars are left out.
     */
    fun pickWindows(wins: List<SWindow>): List<Int> {
        val active = wins.indexOfFirst { it.active }
        if (active < 0) return emptyList()
        val floor = wins[active].layer
        val app = wins[active].pkg ?: return listOf(active)
        val above = wins.indices.filter { i ->
            i != active && wins[i].type == TYPE_APPLICATION && wins[i].layer > floor && wins[i].pkg == app
        }.sortedByDescending { wins[it].layer }
        return above + active
    }

    /** One line: runs of white space become one space. Nothing is cut. */
    private fun line(s: String) = s.replace(Regex("\\s+"), " ").trim()

    private fun shortId(id: String?): String? = id?.substringAfter(":id/")?.takeIf { it.isNotBlank() }

    fun kind(n: SNode): String = when {
        n.editable -> "field"
        n.checkable -> "toggle"
        n.scrollable -> "list"
        else -> "button"
    }

    /** Labels of the non-actionable visible nodes inside n (an unlabeled row's TextViews), in order. */
    private fun innerLabels(n: SNode, out: MutableList<SNode>) {
        for (c in n.children) {
            if (c.actionable && c.visible) continue
            if (c.visible && c.ownLabel().isNotEmpty()) out.add(c)
            innerLabels(c, out)
        }
    }

    /**
     * The screen, compactly: [items] are the things to act on (label, kind, short id, centre), [text]
     * the other text top to bottom, [rev] a revision that changes when what is shown changes. A
     * clickable row with no label of its own takes its inner texts as label and detail. maxItems and
     * maxText below 0 mean every one (the default); given, what is left over is counted in more_items
     * and more_text.
     */
    fun summary(roots: List<SNode>, maxItems: Int = -1, maxText: Int = -1, pkg: String? = null): JSONObject {
        val items = JSONArray()
        val text = JSONArray()
        val used = HashSet<SNode>()
        var moreItems = 0
        var moreText = 0
        var focus: String? = null
        var lastText: String? = null
        fun walk(n: SNode) {
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
                val detail = labels.filter { it != label }
                used.addAll(inner)
                val id = shortId(n.id)
                if (label.isNotEmpty() || id != null || n.editable) {
                    if (maxItems < 0 || items.length() < maxItems) {
                        items.put(JSONObject().apply {
                            put("text", line(label))
                            put("kind", kind(n))
                            if (detail.isNotEmpty()) put("detail", line(detail.joinToString(" · ")))
                            id?.let { put("id", it) }
                            if (n.checkable) put("checked", n.checked)
                            if (n.selected) put("selected", true)
                            if (!n.enabled) put("enabled", false)
                            if (n.editable) {
                                n.value()?.let { put("value", it) }
                                if (n.password) put("password", true)
                                n.hint?.takeIf { it.isNotBlank() && it != label }?.let { put("hint", line(it)) }
                            }
                            if (n.focused && n.editable) put("focused", true)
                            put("x", n.centerX())
                            put("y", n.centerY())
                        })
                    } else moreItems++
                }
                if (n.focused && n.editable) focus = label.ifEmpty { id ?: "field" }
            } else if (n.visible && n !in used) {
                val l = n.ownLabel()
                // A labelled container and its own text say the same thing once.
                if (l.isNotEmpty() && l != lastText) {
                    if (maxText < 0 || text.length() < maxText) text.put(line(l)) else moreText++
                    lastText = l
                }
            }
            for (c in n.children) walk(c)
        }
        roots.forEach { walk(it) }
        return JSONObject().apply {
            pkg?.let { put("package", it) }
            focus?.let { put("focus", it) }
            put("items", items)
            put("text", text)
            if (moreItems > 0) put("more_items", moreItems)
            if (moreText > 0) put("more_text", moreText)
            put("rev", rev(roots))
        }
    }

    /**
     * A short revision of what is shown: visible labels, field values, toggle states and the number of
     * windows. Positions are left out, so an animation that only moves things is not a change.
     */
    fun rev(roots: List<SNode>): String {
        var h = -0x340d631b7bdddcdbL   // FNV-1a 64 offset basis
        fun mix(s: String) {
            for (ch in s) { h = h xor ch.code.toLong(); h *= 0x100000001b3L }
            h = h xor 0x1fL; h *= 0x100000001b3L
        }
        fun walk(n: SNode) {
            if (n.visible) {
                val l = n.ownLabel()
                if (l.isNotEmpty()) mix(l)
                n.value()?.let { mix("=$it") }
                if (n.checkable) mix(if (n.checked) "[x]" else "[ ]")
                if (n.selected) mix("[sel]")
            }
            for (c in n.children) walk(c)
        }
        mix("w${roots.size}")
        roots.forEach { walk(it) }
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

    /** Every node, in tree (top to bottom) order. */
    fun flatten(roots: List<SNode>): List<SNode> {
        val out = ArrayList<SNode>()
        fun walk(n: SNode) { out.add(n); n.children.forEach { walk(it) } }
        roots.forEach { walk(it) }
        return out
    }

    /** ui.click's rule: text matches the node's text, or its description when it has none; desc and id as given. */
    fun matches(n: SNode, text: String?, desc: String?, id: String?, contains: Boolean): Boolean {
        if (text != null) {
            val ok = if (!n.text.isNullOrBlank()) same(n.text, text, contains) else same(n.desc, text, contains)
            if (!ok) return false
        }
        if (desc != null && !same(n.desc, desc, contains)) return false
        if (id != null && !idSame(n.id, id, contains)) return false
        return true
    }

    /** The visible nodes matching text, desc and id, top to bottom. */
    fun find(roots: List<SNode>, text: String?, desc: String?, id: String?, contains: Boolean): List<SNode> =
        flatten(roots).filter { it.visible && matches(it, text, desc, id, contains) }

    /** A node as a reply names it. */
    fun describe(n: SNode): JSONObject = JSONObject().apply {
        n.text?.trim()?.takeIf { it.isNotEmpty() }?.let { put("text", it) }
        n.desc?.trim()?.takeIf { it.isNotEmpty() }?.let { put("desc", it) }
        n.id?.let { put("id", it) }
        n.cls?.let { put("class", it) }
        if (n.clickable) put("clickable", true)
        if (n.longClickable) put("long_clickable", true)
        if (n.scrollable) put("scrollable", true)
        if (n.checkable) put("checked", n.checked)
        if (n.selected) put("selected", true)
        if (!n.enabled) put("enabled", false)
        if (n.editable) put("editable", true)
        put("depth", n.depth)
        put("bounds", JSONArray(n.bounds.toList()))
    }

    private fun label(n: SNode): String = n.text?.trim()?.takeIf { it.isNotEmpty() } ?: n.desc?.trim() ?: ""

    /** For a not-found error: every visible node that shares a word with the query, else every label on screen. */
    fun near(roots: List<SNode>, text: String?, desc: String?, id: String?): JSONArray {
        val words = listOfNotNull(text, desc, id).flatMap { norm(it).split(Regex("[^a-z0-9]+")) }.filter { it.length >= 3 }
        val visible = flatten(roots).filter { it.visible }
        val out = JSONArray()
        val seen = HashSet<String>()
        for (n in visible) {
            val hay = norm(label(n)) + " " + norm(n.id)
            if (hay.isBlank() || words.none { hay.contains(it) }) continue
            if (seen.add(label(n).ifEmpty { n.id ?: "" })) out.put(describe(n))
        }
        if (out.length() == 0) {
            for (n in visible) {
                val l = label(n)
                if (l.isNotEmpty() && seen.add(l)) out.put(l)
            }
        }
        return out
    }

    /** What ui.click clicks for match [hit]: its nearest clickable (or long-clickable) self or parent, or null. */
    fun clickTarget(hit: SNode, long: Boolean): SNode? {
        var t: SNode? = hit
        while (t != null && !(if (long) t.longClickable else t.clickable)) t = t.parent
        return t
    }

    /** What ui.scroll scrolls: the scrollable holding [hit], or with none the outermost scrollable (breadth first, top window first). */
    fun scrollTarget(roots: List<SNode>, hit: SNode?): SNode? {
        if (hit != null) {
            var t: SNode? = hit
            while (t != null && !t.scrollable) t = t.parent
            return t
        }
        val queue = ArrayDeque(roots)
        while (queue.isNotEmpty()) {
            val n = queue.removeFirst()
            if (n.scrollable && n.visible) return n
            queue.addAll(n.children)
        }
        return null
    }

    /** Visible editable fields, top to bottom. */
    fun fields(roots: List<SNode>): List<SNode> = flatten(roots).filter { it.visible && it.editable }

    /**
     * Which of [hits] ui.type uses: [index] (0 = top), or with index < 0 the only one. The position, or
     * null with the reason when there are several and no index, none, or the index is out of range.
     */
    fun chooseField(hits: List<SNode>, index: Int): Pair<Int?, String?> = when {
        index < 0 && hits.size > 1 -> null to "${hits.size} fields match; pass index (0 = top)"
        hits.isEmpty() -> null to "no field matches"
        index < 0 -> 0 to null
        index >= hits.size -> null to "only ${hits.size} matching field(s), index $index out of range"
        else -> index to null
    }

    /** The text to set: [text], or the field's value plus it; null when appending to a password (unreadable). */
    fun newText(f: SNode, text: String, append: Boolean): String? = when {
        !append -> text
        f.password -> null
        else -> (f.value() ?: "") + text
    }

    /** How a field is named in a reply: label, description, hint or id. */
    fun fieldName(n: SNode): String =
        listOf(n.labeledBy, n.desc, n.hint, shortId(n.id)).firstOrNull { !it.isNullOrBlank() }?.trim() ?: "field"

    /**
     * Fields named by [label] (its hint, description, labelFor view, or a text just above it or to its
     * left on the same row; never what is typed in it) and/or [id]. With neither: the focused field,
     * else the only one on screen. "Just above" is 0.5's: the nearest field starting within 300 px
     * below the caption and overlapping it sideways.
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
                    same(f.labeledBy, label, contains))
        }
        if (direct.isNotEmpty() || label == null) return direct
        val out = LinkedHashSet<SNode>()
        for (t in flatten(roots)) {
            if (!t.visible || t.editable || !same(t.ownLabel(), label, contains)) continue
            val b = t.bounds
            val near = all.filter { f ->
                val fb = f.bounds
                val below = fb[1] >= b[1] && fb[1] - b[3] <= 300 && fb[0] < b[2] && fb[2] > b[0]
                val right = fb[0] >= b[2] - 4 && fb[1] < b[3] && fb[3] > b[1]
                (id == null || idSame(f.id, id, contains)) && (below || right)
            }.minByOrNull { f -> Math.abs(f.bounds[1] - b[1]) + Math.abs(f.bounds[0] - b[0]) }
            near?.let { out.add(it) }
        }
        return out.toList()
    }

    /**
     * The lock screen's PIN pad: digit -> its key, found by view id (key0 ... key9), else by a digit as a clickable's
     * text. Found by text, it is a pad only when every digit is there: a lock screen's notifications show single
     * digits too (a group's count, a timer's), and those are not keys (brief AP8: after a reboot they are common).
     */
    fun pinKeys(roots: List<SNode>): Map<Char, SNode> {
        val out = HashMap<Char, SNode>()
        val all = flatten(roots).filter { it.visible }
        for (n in all) {
            val short = shortId(n.id) ?: continue
            if (short.length == 4 && short.startsWith("key") && short[3].isDigit()) out.putIfAbsent(short[3], n)
        }
        if (out.size < 10) {
            val byText = HashMap(out)
            for (n in all) {
                val t = n.text?.trim() ?: n.desc?.trim() ?: continue
                if (t.length == 1 && t[0].isDigit() && clickTarget(n, false) != null) byText.putIfAbsent(t[0], n)
            }
            if (byText.size == 10) return byText
        }
        return out
    }

    /** How many characters the PIN pad's field holds (its dots: a password's length is shown, never its text), or -1 with no field. */
    fun pinLength(roots: List<SNode>): Int {
        val all = flatten(roots).filter { it.visible && it.editable }
        val field = all.firstOrNull { shortId(it.id) == "pinEntry" } ?: all.firstOrNull { it.password } ?: return -1
        return field.text?.length ?: 0
    }

    /** The lock screen's own way to its PIN pad when none shows: its lock icon (AOSP's device_entry_icon, "Device locked"). */
    fun unlockIcon(roots: List<SNode>): SNode? = flatten(roots).filter { it.visible }.firstOrNull { n ->
        val id = shortId(n.id) ?: ""
        id.startsWith("device_entry_icon") || id.startsWith("lock_icon") || norm(label(n)) in setOf("device locked", "unlock")
    }

    /** The PIN pad's enter key: view id key_enter, else a clickable described as enter or OK. */
    fun enterKey(roots: List<SNode>): SNode? {
        val all = flatten(roots).filter { it.visible }
        return all.firstOrNull { shortId(it.id) == "key_enter" }
            ?: all.firstOrNull { n -> clickTarget(n, false) != null && norm(label(n)) in setOf("enter", "ok", "done") }
    }
}

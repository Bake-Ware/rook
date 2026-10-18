package systems.bake.rook

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.AccessibilityServiceInfo
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.graphics.Rect
import android.os.Build
import android.os.Bundle
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import org.json.JSONArray
import org.json.JSONObject

/**
 * Rootless HID backend. Python's `hid.*` capabilities call the static helpers
 * here; they proxy to the running AccessibilityService instance (set in
 * [onServiceConnected]). Returns false if the service isn't enabled yet.
 *
 * Taps/swipes use dispatchGesture; text goes to the focused editable node via
 * ACTION_SET_TEXT (works without IME hacks).
 *
 * The node helpers (ui.nodes / ui.click / ui.scroll) act on labels instead of
 * pixels, so they work in apps that block screenshots (FLAG_SECURE).
 */
class HidAccessibilityService : AccessibilityService() {

    override fun onServiceConnected() {
        super.onServiceConnected()
        // View ids make ui.nodes / ui.click usable by id. Set here as well as in
        // the XML so a service bound before the XML change still reports them.
        serviceInfo = serviceInfo?.apply { flags = flags or AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS }
        instance = this
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) { /* unused */ }
    override fun onInterrupt() { /* unused */ }

    private fun tapInternal(x: Float, y: Float): Boolean {
        val path = Path().apply { moveTo(x, y) }
        val stroke = GestureDescription.StrokeDescription(path, 0, 50)
        return dispatchGesture(GestureDescription.Builder().addStroke(stroke).build(), null, null)
    }

    private fun swipeInternal(x1: Float, y1: Float, x2: Float, y2: Float, ms: Long): Boolean {
        val path = Path().apply { moveTo(x1, y1); lineTo(x2, y2) }
        val stroke = GestureDescription.StrokeDescription(path, 0, ms)
        return dispatchGesture(GestureDescription.Builder().addStroke(stroke).build(), null, null)
    }

    private fun typeInternal(text: String): Boolean {
        val root = rootInActiveWindow ?: return false
        val node = findFocusedEditable(root) ?: return false
        val args = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        }
        return node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
    }

    private fun dumpTextInternal(): String {
        val root = rootInActiveWindow ?: return ""
        val sb = StringBuilder()
        collectText(root, sb, 0)
        return sb.toString().trim()
    }

    private fun collectText(node: AccessibilityNodeInfo?, sb: StringBuilder, depth: Int) {
        node ?: return
        if (depth > 40) return
        val t = node.text?.toString()?.trim()
        val d = node.contentDescription?.toString()?.trim()
        if (!t.isNullOrEmpty()) { sb.append(t); sb.append('\n') }
        else if (!d.isNullOrEmpty()) { sb.append(d); sb.append('\n') }
        for (i in 0 until node.childCount) {
            collectText(node.getChild(i), sb, depth + 1)
        }
    }


    // ---- label-based node access (ui.nodes / ui.click / ui.scroll) ----

    private class Hit(val node: AccessibilityNodeInfo, val depth: Int)

    /** Visible nodes of the active window in tree (top-to-bottom) order. */
    private fun visibleNodes(): List<Hit> {
        val root = rootInActiveWindow ?: return emptyList()
        val out = ArrayList<Hit>()
        fun walk(n: AccessibilityNodeInfo?, depth: Int) {
            n ?: return
            if (depth > 60 || out.size > 4000) return
            if (n.isVisibleToUser) out.add(Hit(n, depth))
            for (i in 0 until n.childCount) walk(n.getChild(i), depth + 1)
        }
        walk(root, 0)
        return out
    }

    private fun label(n: AccessibilityNodeInfo): String {
        val t = n.text?.toString()?.trim()
        if (!t.isNullOrEmpty()) return t
        return n.contentDescription?.toString()?.trim() ?: ""
    }

    private fun describe(n: AccessibilityNodeInfo, depth: Int = -1): JSONObject {
        val r = Rect().also { n.getBoundsInScreen(it) }
        return JSONObject().apply {
            n.text?.toString()?.trim()?.takeIf { it.isNotEmpty() }?.let { put("text", it) }
            n.contentDescription?.toString()?.trim()?.takeIf { it.isNotEmpty() }?.let { put("desc", it) }
            n.viewIdResourceName?.let { put("id", it) }
            n.className?.let { put("class", it.toString().substringAfterLast('.')) }
            if (n.isClickable) put("clickable", true)
            if (n.isLongClickable) put("long_clickable", true)
            if (n.isScrollable) put("scrollable", true)
            if (n.isCheckable) put("checked", n.isChecked)
            if (n.isSelected) put("selected", true)
            if (!n.isEnabled) put("enabled", false)
            if (n.isEditable) put("editable", true)
            if (depth >= 0) put("depth", depth)
            put("bounds", JSONArray(listOf(r.left, r.top, r.right, r.bottom)))
        }
    }

    private fun nodesInternal(all: Boolean, limit: Int): String {
        val out = JSONArray()
        for (h in visibleNodes()) {
            val n = h.node
            val interesting = !n.text.isNullOrBlank() || !n.contentDescription.isNullOrBlank() ||
                n.isClickable || n.isScrollable || n.isEditable || n.isCheckable
            if (!all && !interesting) continue
            out.put(describe(n, h.depth))
            if (out.length() >= limit) break
        }
        return out.toString()
    }

    private fun norm(s: String?) = s?.trim()?.lowercase() ?: ""

    private fun idMatches(nodeId: String?, want: String, contains: Boolean): Boolean {
        val full = norm(nodeId)
        if (full.isEmpty()) return false
        val w = norm(want)
        if (contains) return full.contains(w)
        return full == w || full.substringAfter(":id/") == w
    }

    private fun textMatches(have: String?, want: String, contains: Boolean): Boolean {
        val h = norm(have)
        if (h.isEmpty()) return false
        val w = norm(want)
        return if (contains) h.contains(w) else h == w
    }

    /** `text` matches the node text, or its content description when it has no text. */
    private fun matches(n: AccessibilityNodeInfo, text: String?, desc: String?, id: String?, contains: Boolean): Boolean {
        if (text != null) {
            val t = n.text?.toString()
            val ok = if (!t.isNullOrBlank()) textMatches(t, text, contains)
                     else textMatches(n.contentDescription?.toString(), text, contains)
            if (!ok) return false
        }
        if (desc != null && !textMatches(n.contentDescription?.toString(), desc, contains)) return false
        if (id != null && !idMatches(n.viewIdResourceName, id, contains)) return false
        return true
    }

    private fun findMatches(text: String?, desc: String?, id: String?, contains: Boolean): List<AccessibilityNodeInfo> =
        visibleNodes().map { it.node }.filter { matches(it, text, desc, id, contains) }

    /** Labels that look like the query, for a useful not-found error. */
    private fun nearMatches(text: String?, desc: String?, id: String?): JSONArray {
        val words = listOfNotNull(text, desc, id).flatMap { norm(it).split(Regex("[^a-z0-9]+")) }
            .filter { it.length >= 3 }
        val out = JSONArray()
        val seen = HashSet<String>()
        for (h in visibleNodes()) {
            val n = h.node
            val hay = norm(label(n)) + " " + norm(n.viewIdResourceName)
            if (hay.isBlank() || words.none { hay.contains(it) }) continue
            val l = label(n).ifEmpty { n.viewIdResourceName ?: "" }
            if (seen.add(l)) out.put(describe(n))
            if (out.length() >= 10) break
        }
        if (out.length() == 0) {   // nothing similar: show the first labels on screen
            for (h in visibleNodes()) {
                val l = label(h.node)
                if (l.isNotEmpty() && seen.add(l)) out.put(l)
                if (out.length() >= 15) break
            }
        }
        return out
    }

    private fun notFound(text: String?, desc: String?, id: String?, count: Int, index: Int): String =
        JSONObject().apply {
            put("ok", false)
            put("error", if (count == 0) "no visible node matches" else "only $count match(es), index $index out of range")
            put("matches", count)
            put("near", nearMatches(text, desc, id))
        }.toString()

    private fun clickInternal(text: String?, desc: String?, id: String?, contains: Boolean, index: Int, long: Boolean): String {
        if (text == null && desc == null && id == null)
            return JSONObject().put("ok", false).put("error", "give text, desc or id").toString()
        val hits = findMatches(text, desc, id, contains)
        if (index < 0 || index >= hits.size) return notFound(text, desc, id, hits.size, index)
        val node = hits[index]
        var target: AccessibilityNodeInfo? = node
        while (target != null && !(if (long) target.isLongClickable else target.isClickable)) target = target.parent
        val out = JSONObject().put("matches", hits.size).put("clicked", describe(node))
        if (target != null) {
            val action = if (long) AccessibilityNodeInfo.ACTION_LONG_CLICK else AccessibilityNodeInfo.ACTION_CLICK
            val ok = target.performAction(action)
            out.put("ok", ok).put("via", "action")
            if (target !== node) out.put("target", describe(target))
            if (!ok) out.put("error", "performAction refused")
            return out.toString()
        }
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.N)
            return out.put("ok", false).put("error", "no clickable ancestor and gestures need API 24").toString()
        val r = Rect().also { node.getBoundsInScreen(it) }
        val path = Path().apply { moveTo(r.exactCenterX(), r.exactCenterY()) }
        val stroke = GestureDescription.StrokeDescription(path, 0, if (long) 800L else 50L)
        val ok = dispatchGesture(GestureDescription.Builder().addStroke(stroke).build(), null, null)
        out.put("ok", ok).put("via", "gesture").put("tap", JSONArray(listOf(r.centerX(), r.centerY())))
        return out.toString()
    }

    private fun scrollInternal(forward: Boolean, text: String?, id: String?, contains: Boolean): String {
        val out = JSONObject()
        var target: AccessibilityNodeInfo?
        if (text != null || id != null) {
            val hits = findMatches(text, null, id, contains)
            if (hits.isEmpty()) return notFound(text, null, id, 0, 0)
            target = hits[0]
            while (target != null && !target.isScrollable) target = target.parent
        } else {
            // Breadth-first, so the outermost (main) list wins over nested carousels.
            target = null
            val root = rootInActiveWindow
            val queue = ArrayDeque<AccessibilityNodeInfo>()
            root?.let { queue.add(it) }
            while (queue.isNotEmpty()) {
                val n = queue.removeFirst()
                if (n.isScrollable && n.isVisibleToUser) { target = n; break }
                for (i in 0 until n.childCount) n.getChild(i)?.let { queue.add(it) }
            }
        }
        if (target == null) return out.put("ok", false).put("scrolled", false).put("error", "no scrollable node").toString()
        val action = if (forward) AccessibilityNodeInfo.ACTION_SCROLL_FORWARD else AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
        val ok = target.performAction(action)
        return out.put("ok", true).put("scrolled", ok).put("target", describe(target)).toString()
    }

    private fun findFocusedEditable(node: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        if (node.isEditable && node.isFocused) return node
        for (i in 0 until node.childCount) {
            node.getChild(i)?.let { child ->
                findFocusedEditable(child)?.let { return it }
            }
        }
        return null
    }

    companion object {
        @Volatile private var instance: HidAccessibilityService? = null

        @JvmStatic fun isEnabled(): Boolean = instance != null

        // Gesture dispatch is API 24+. On Android 6/7-without-N it's unavailable;
        // callers get false (text input + global actions still work).
        private fun gesturesOk(): Boolean = Build.VERSION.SDK_INT >= Build.VERSION_CODES.N

        @JvmStatic fun tap(x: Int, y: Int): Boolean =
            if (!gesturesOk()) false else instance?.tapInternal(x.toFloat(), y.toFloat()) ?: false

        @JvmStatic fun swipe(x1: Int, y1: Int, x2: Int, y2: Int, durationMs: Long): Boolean =
            if (!gesturesOk()) false
            else instance?.swipeInternal(x1.toFloat(), y1.toFloat(), x2.toFloat(), y2.toFloat(), durationMs) ?: false

        @JvmStatic fun typeText(text: String): Boolean =
            instance?.typeInternal(text) ?: false

        @JvmStatic fun back(): Boolean = instance?.performGlobalAction(GLOBAL_ACTION_BACK) ?: false
        @JvmStatic fun home(): Boolean = instance?.performGlobalAction(GLOBAL_ACTION_HOME) ?: false
        @JvmStatic fun recents(): Boolean = instance?.performGlobalAction(GLOBAL_ACTION_RECENTS) ?: false

        /** All visible text on the current screen, or null if not enabled. */
        @JvmStatic fun screenText(): String? = instance?.dumpTextInternal()

        /** JSON array of visible nodes (see describe()), or null if not enabled. */
        @JvmStatic fun screenNodes(all: Boolean, limit: Int): String? = instance?.nodesInternal(all, limit)

        /** Click the [index]th node matching text/desc/id; JSON result, or null if not enabled. */
        @JvmStatic fun clickNode(text: String?, desc: String?, id: String?, contains: Boolean, index: Int, long: Boolean): String? =
            instance?.clickInternal(text, desc, id, contains, index, long)

        /** Scroll the matched (or first) scrollable node; JSON result, or null if not enabled. */
        @JvmStatic fun scrollNode(forward: Boolean, text: String?, id: String?, contains: Boolean): String? =
            instance?.scrollInternal(forward, text, id, contains)

        /** Package and label of the app in the active window, or null. */
        @JvmStatic fun screenApp(): Array<String>? = instance?.let { svc ->
            val pkg = svc.rootInActiveWindow?.packageName?.toString() ?: return@let null
            val label = try {
                val pm = svc.packageManager
                pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString()
            } catch (_: Exception) { pkg }
            arrayOf(pkg, label)
        }
    }
}

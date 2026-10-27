package systems.bake.rook

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.AccessibilityServiceInfo
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.graphics.Rect
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
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
        // Interactive windows: a dropdown or popup menu is its own window above
        // the activity, which rootInActiveWindow alone never shows.
        serviceInfo = serviceInfo?.apply {
            flags = flags or AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS or
                AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
        }
        lastChange = SystemClock.uptimeMillis()
        instance = this
        KeepAwake.accessibilityChanged()
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        KeepAwake.accessibilityChanged()
        super.onDestroy()
    }

    /** Only notes when the screen last changed (ui.wait's "settled"); nothing else runs here. */
    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        when (event?.eventType) {
            AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED, AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED,
            AccessibilityEvent.TYPE_WINDOWS_CHANGED, AccessibilityEvent.TYPE_VIEW_SCROLLED ->
                lastChange = SystemClock.uptimeMillis()
        }
    }
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
        val node = findFocus(AccessibilityNodeInfo.FOCUS_INPUT)?.takeIf { it.isEditable }
            ?: screenRoots().firstNotNullOfOrNull { findFocusedEditable(it) } ?: return false
        val args = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        }
        return node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
    }

    private fun dumpTextInternal(): String {
        val roots = screenRoots()
        if (roots.isEmpty()) return ""
        val sb = StringBuilder()
        for (root in roots) collectText(root, sb, 0)
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

    /**
     * Roots of the windows the ui.* caps read: the active window, then any app
     * window above it (dropdowns, popup menus, dialogs). See ScreenModel.pickWindows.
     */
    private fun screenRoots(): List<AccessibilityNodeInfo> {
        val active = rootInActiveWindow
        val wins: List<AccessibilityWindowInfo> = try { windows ?: emptyList() } catch (_: Exception) { emptyList() }
        if (wins.isEmpty()) return listOfNotNull(active)
        // Roots of app windows only (popups, dialogs, the activity): their package
        // keeps another app's window out (ScreenModel.pickWindows).
        val roots = wins.map { w ->
            if (w.isActive) active ?: w.root
            else if (w.type == AccessibilityWindowInfo.TYPE_APPLICATION) w.root else null
        }
        val picked = ScreenModel.pickWindows(wins.mapIndexed { i, w ->
            SWindow(w.type, w.layer, w.isActive, roots[i]?.packageName?.toString())
        })
        if (picked.isEmpty()) return listOfNotNull(active)
        return picked.mapNotNull { roots[it] }.distinct()
    }

    /** Visible nodes of the screen's windows in tree (top-to-bottom) order. */
    private fun visibleNodes(): List<Hit> {
        val roots = screenRoots()
        if (roots.isEmpty()) return emptyList()
        val out = ArrayList<Hit>()
        fun walk(n: AccessibilityNodeInfo?, depth: Int) {
            n ?: return
            if (depth > 60 || out.size > 4000) return
            if (n.isVisibleToUser) out.add(Hit(n, depth))
            for (i in 0 until n.childCount) walk(n.getChild(i), depth + 1)
        }
        for (root in roots) walk(root, 0)
        return out
    }

    /** The screen copied once into plain nodes (ScreenModel), with live refs to act on. */
    private fun snapshot(): List<SNode> {
        var count = 0
        fun copy(n: AccessibilityNodeInfo, depth: Int): SNode {
            count++
            val kids = ArrayList<SNode>()
            if (depth < 60) {
                for (i in 0 until n.childCount) {
                    if (count > 4000) break
                    n.getChild(i)?.let { kids.add(copy(it, depth + 1)) }
                }
            }
            val r = Rect().also { n.getBoundsInScreen(it) }
            val hint = if (Build.VERSION.SDK_INT >= 26) n.hintText?.toString() else null
            return SNode(
                text = n.text?.toString(), desc = n.contentDescription?.toString(),
                id = n.viewIdResourceName, cls = n.className?.toString()?.substringAfterLast('.'),
                hint = hint, labeledBy = n.labeledBy?.let { label(it) }?.takeIf { it.isNotEmpty() },
                clickable = n.isClickable, longClickable = n.isLongClickable, scrollable = n.isScrollable,
                editable = n.isEditable, checkable = n.isCheckable, checked = n.isChecked,
                selected = n.isSelected, focused = n.isFocused, password = n.isPassword,
                showingHint = Build.VERSION.SDK_INT >= 26 && n.isShowingHintText,
                enabled = n.isEnabled, visible = n.isVisibleToUser,
                bounds = intArrayOf(r.left, r.top, r.right, r.bottom), children = kids, ref = n,
            )
        }
        return screenRoots().map { copy(it, 0) }
    }

    private fun summaryInternal(maxItems: Int, maxText: Int): String {
        // The package from the same read as the items, so they never mix two moments.
        val roots = snapshot()
        val pkg = (roots.lastOrNull()?.ref as? AccessibilityNodeInfo)?.packageName?.toString()
        return ScreenModel.summary(roots, maxItems, maxText, pkg).toString()
    }

    private fun revInternal(): String = ScreenModel.rev(snapshot())

    /** ui.type: set text in the field named by label/id (else the focused or only one). */
    private fun typeIntoInternal(label: String?, id: String?, contains: Boolean, index: Int,
                                 text: String, append: Boolean, submit: Boolean): String {
        val roots = snapshot()
        val hits = ScreenModel.findFields(roots, label, id, contains)
        val out = JSONObject()
        val (pick, why) = ScreenModel.chooseField(hits, index)
        if (pick == null) {
            val names = JSONArray()
            (if (hits.size > 1) hits else ScreenModel.fields(roots)).take(10).forEach {
                names.put(JSONObject().put("label", ScreenModel.fieldName(it))
                    .put("bounds", JSONArray(it.bounds.toList())))
            }
            val err = if (hits.isEmpty() && label == null && id == null) {
                if (names.length() == 0) "no text field on screen" else "no focused field; name one with label or id"
            } else why
            return out.put("ok", false).put("error", err).put("matches", hits.size).put("fields", names).toString()
        }
        val f = hits[pick]
        val node = f.ref as? AccessibilityNodeInfo ?: return out.put("ok", false).put("error", "field went away").toString()
        val now = ScreenModel.newText(f, text, append)
            ?: return out.put("ok", false).put("error", "can't append to a password field (its text can't be read); type it whole").toString()
        node.performAction(AccessibilityNodeInfo.ACTION_FOCUS)
        val args = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, now)
        }
        val ok = node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
        out.put("ok", ok).put("matches", hits.size)
            .put("field", JSONObject().apply {
                put("label", ScreenModel.fieldName(f))
                f.id?.let { put("id", it) }
                if (f.password) put("password", true)
                put("bounds", JSONArray(f.bounds.toList()))
            })
            .put("chars", text.length)
        if (!ok) out.put("error", "the field refused the text")
        if (ok && submit) {
            if (Build.VERSION.SDK_INT >= 30) {
                val sent = node.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.id)
                out.put("submitted", sent)
                if (!sent) out.put("note", "the field has no enter action; ui.click its send button")
            } else {
                out.put("submitted", false).put("note", "submit needs Android 11+; ui.click the send button")
            }
        }
        return out.toString()
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
            val queue = ArrayDeque<AccessibilityNodeInfo>()
            queue.addAll(screenRoots())   // top window first: an open dropdown's list
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
        /** SystemClock.uptimeMillis() of the last window/content change event. */
        @Volatile private var lastChange: Long = 0L

        @JvmStatic fun isEnabled(): Boolean = instance != null

        /** Context for TYPE_ACCESSIBILITY_OVERLAY windows (keep-awake), or null if not enabled. */
        fun overlayContext(): android.content.Context? = instance

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

        /** Compact screen JSON (ScreenModel.summary), or null if not enabled. */
        @JvmStatic fun screenSummary(maxItems: Int, maxText: Int): String? = instance?.summaryInternal(maxItems, maxText)

        /** Revision of what is on screen (changes when labels/values/toggles do), or null. */
        @JvmStatic fun screenRev(): String? = instance?.revInternal()

        /** Milliseconds since the screen last changed, or -1 if not enabled. */
        @JvmStatic fun quietMs(): Long =
            if (instance == null) -1L else SystemClock.uptimeMillis() - lastChange

        /** How many visible nodes match text/desc/id (ui.click's rules), or -1 if not enabled. */
        @JvmStatic fun matchCount(text: String?, desc: String?, id: String?, contains: Boolean): Int =
            instance?.findMatches(text, desc, id, contains)?.size ?: -1

        /** How many fields ui.type would pick for label/id, or -1 if not enabled. */
        @JvmStatic fun fieldCount(label: String?, id: String?, contains: Boolean): Int =
            instance?.let { ScreenModel.findFields(it.snapshot(), label, id, contains).size } ?: -1

        /** ui.type; JSON result, or null if not enabled. */
        @JvmStatic fun typeInto(label: String?, id: String?, contains: Boolean, index: Int,
                                text: String, append: Boolean, submit: Boolean): String? =
            instance?.typeIntoInternal(label, id, contains, index, text, append, submit)

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

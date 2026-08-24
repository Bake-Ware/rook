package systems.bake.rook

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.os.Build
import android.os.Bundle
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

/**
 * Rootless HID backend. Python's `hid.*` capabilities call the static helpers
 * here; they proxy to the running AccessibilityService instance (set in
 * [onServiceConnected]). Returns false if the service isn't enabled yet.
 *
 * Taps/swipes use dispatchGesture; text goes to the focused editable node via
 * ACTION_SET_TEXT (works without IME hacks).
 */
class HidAccessibilityService : AccessibilityService() {

    override fun onServiceConnected() {
        super.onServiceConnected()
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
    }
}

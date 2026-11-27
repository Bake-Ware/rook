package systems.bake.rook.core.worker

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.AccessibilityServiceInfo
import android.accessibilityservice.GestureDescription
import android.app.KeyguardManager
import android.graphics.Bitmap
import android.graphics.Path
import android.graphics.Rect
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.view.Display
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.CountDownLatch

/**
 * The phone's hands and eyes (brief AP5): one accessibility service that does the gestures, the
 * global keys and the typing, reads what is on screen for the ui caps, and, from Android 11, takes
 * screenshots with no prompt. The Python worker parts (hid-android, ui-android, screen-android,
 * phone, app-android) call the static functions below through Chaquopy, off the worker's event loop;
 * each blocks for as long as its work takes. Bridge.hands(cap) first makes sure the service is on,
 * asking the person when it is not.
 *
 *     Hands.on()                                   is the service on
 *     Hands.screen(maxItems, maxText)              the screen, compactly (Screen.summary), as JSON
 *     Hands.rev(), Hands.front(), Hands.locked()   what is shown, which app is in front, the lock screen
 *     Hands.changes(), Hands.quietMs(), Hands.now(), Hands.awaitChange(seen, ms)
 *     Hands.matchCount(...), Hands.fieldCount(...)
 *     Hands.click(...), Hands.scroll(...), Hands.typeInto(...), Hands.typeFocused(text)
 *     Hands.after(rev, app, since, maxItems, maxText)    once an action's change has settled, the screen
 *     Hands.tap(x, y), Hands.swipe(x1, y1, x2, y2, ms), Hands.press(key)
 *     Hands.typePin(pin), Hands.pressEnter(), Hands.pressUnlockIcon()   the lock screen's PIN pad
 *     Hands.screenshot(quality)                    a JPEG (Android 11 and later)
 *
 * The screen model is Screen.kt's (0.5's ScreenModel, uncut). Nothing here limits what is read,
 * clicked or typed: every node on screen is read, every match is named. What Android itself refuses
 * (a password field's text, a secure window's picture) is reported as Android says it.
 */
class Hands : AccessibilityService() {

    override fun onServiceConnected() {
        super.onServiceConnected()
        // View ids, and the windows above the active one (dropdowns, popups), as 0.5 asked for them.
        serviceInfo = serviceInfo?.apply {
            flags = flags or AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS or
                AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
        }
        Bridge.attach(this)
        instance = this
        changed()
        Bridge.handsChanged()
    }

    override fun onUnbind(intent: android.content.Intent?): Boolean {
        gone()
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        gone()
        super.onDestroy()
    }

    private fun gone() {
        if (instance === this) instance = null
        Bridge.handsChanged()
    }

    /** Only notes that the screen changed (ui.wait, and "settled"); nothing else runs here. */
    override fun onAccessibilityEvent(event: AccessibilityEvent?) = changed()

    override fun onInterrupt() {}

    // ---- reading ----

    /** Roots of the windows the ui caps read (Screen.pickWindows): the active window and its app's windows above it. */
    private fun roots(): List<AccessibilityNodeInfo> {
        val active = rootInActiveWindow
        val wins: List<AccessibilityWindowInfo> = try { windows ?: emptyList() } catch (_: Exception) { emptyList() }
        if (wins.isEmpty()) return listOfNotNull(active)
        val roots = wins.map { w ->
            if (w.isActive) active ?: w.root
            else if (w.type == AccessibilityWindowInfo.TYPE_APPLICATION) w.root else null
        }
        val picked = Screen.pickWindows(wins.mapIndexed { i, w ->
            SWindow(w.type, w.layer, w.isActive, roots[i]?.packageName?.toString())
        })
        if (picked.isEmpty()) return listOfNotNull(active)
        return picked.mapNotNull { roots[it] }.distinct()
    }

    /** The screen copied once into plain nodes, each with its live node to act on. */
    private fun snapshot(): List<SNode> {
        fun copy(n: AccessibilityNodeInfo): SNode {
            val kids = ArrayList<SNode>()
            for (i in 0 until n.childCount) n.getChild(i)?.let { kids.add(copy(it)) }
            val r = Rect().also { n.getBoundsInScreen(it) }
            return SNode(
                text = n.text?.toString(), desc = n.contentDescription?.toString(),
                id = n.viewIdResourceName, cls = n.className?.toString()?.substringAfterLast('.'),
                hint = if (Build.VERSION.SDK_INT >= 26) n.hintText?.toString() else null,
                labeledBy = n.labeledBy?.let { l -> (l.text ?: l.contentDescription)?.toString()?.trim() }?.takeIf { it.isNotEmpty() },
                clickable = n.isClickable, longClickable = n.isLongClickable, scrollable = n.isScrollable,
                editable = n.isEditable, checkable = n.isCheckable, checked = n.isChecked,
                selected = n.isSelected, focused = n.isFocused, password = n.isPassword,
                showingHint = Build.VERSION.SDK_INT >= 26 && n.isShowingHintText,
                enabled = n.isEnabled, visible = n.isVisibleToUser,
                bounds = intArrayOf(r.left, r.top, r.right, r.bottom), children = kids, ref = n,
            )
        }
        return Screen.link(roots().map { copy(it) })
    }

    private fun pkg(roots: List<SNode>) = (roots.lastOrNull()?.ref as? AccessibilityNodeInfo)?.packageName?.toString()

    private fun front(): JSONObject {
        val out = JSONObject()
        val pkg = rootInActiveWindow?.packageName?.toString() ?: return out
        val label = try {
            packageManager.getApplicationLabel(packageManager.getApplicationInfo(pkg, 0)).toString()
        } catch (_: Exception) { pkg }
        return out.put("package", pkg).put("app", label)
    }

    private fun isLocked() = getSystemService(KeyguardManager::class.java).isKeyguardLocked

    private fun screen(maxItems: Int, maxText: Int): JSONObject {
        val roots = snapshot()
        val shot = Screen.summary(roots, maxItems, maxText, pkg(roots))
        val out = front()
        // An app label read a moment apart from another app's items is dropped (0.5).
        if (shot.has("package") && out.optString("package", shot.getString("package")) != shot.getString("package")) out.remove("app")
        if (isLocked()) out.put("locked", true)
        for (k in shot.keys()) out.put(k, shot.get(k))
        if (!out.has("app")) out.put("app", out.optString("package", ""))
        return out
    }

    // ---- acting ----

    private fun gesture(path: Path, ms: Long): Boolean {
        val done = CountDownLatch(1)
        var ok = false
        val sent = dispatchGesture(
            GestureDescription.Builder().addStroke(GestureDescription.StrokeDescription(path, 0, ms)).build(),
            object : GestureResultCallback() {
                override fun onCompleted(g: GestureDescription?) { ok = true; done.countDown() }
                override fun onCancelled(g: GestureDescription?) { done.countDown() }
            }, null)
        if (!sent) return false
        done.await()
        return ok
    }

    private fun tapAt(x: Float, y: Float, ms: Long) = gesture(Path().apply { moveTo(x, y) }, ms)

    private fun act(n: SNode, action: Int, args: Bundle? = null) = (n.ref as AccessibilityNodeInfo).performAction(action, args)

    private fun setText(n: SNode, text: String): Boolean {
        act(n, AccessibilityNodeInfo.ACTION_FOCUS)
        return act(n, AccessibilityNodeInfo.ACTION_SET_TEXT, Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        })
    }

    private fun notFound(roots: List<SNode>, text: String?, desc: String?, id: String?, count: Int, index: Int) =
        JSONObject().put("ok", false)
            .put("error", if (count == 0) "no visible node matches" else "only $count match(es), index $index out of range")
            .put("matches", count).put("near", Screen.near(roots, text, desc, id))

    private fun click(text: String?, desc: String?, id: String?, contains: Boolean, index: Int, long: Boolean): JSONObject {
        if (text == null && desc == null && id == null) return JSONObject().put("ok", false).put("error", "give text, desc or id")
        val roots = snapshot()
        val hits = Screen.find(roots, text, desc, id, contains)
        if (index < 0 || index >= hits.size) return notFound(roots, text, desc, id, hits.size, index)
        val node = hits[index]
        val out = JSONObject().put("matches", hits.size).put("clicked", Screen.describe(node))
        val target = Screen.clickTarget(node, long)
        if (target != null) {
            val ok = act(target, if (long) AccessibilityNodeInfo.ACTION_LONG_CLICK else AccessibilityNodeInfo.ACTION_CLICK)
            out.put("ok", ok).put("via", "action")
            if (target !== node) out.put("target", Screen.describe(target))
            if (!ok) out.put("error", "the app refused the click")
            return out
        }
        val ok = tapAt(node.centerX().toFloat(), node.centerY().toFloat(), if (long) LONG_PRESS_MS else TAP_MS)
        return out.put("ok", ok).put("via", "gesture").put("tap", JSONArray(listOf(node.centerX(), node.centerY())))
            .apply { if (!ok) put("error", "Android cancelled the tap") }
    }

    private fun scroll(forward: Boolean, text: String?, id: String?, contains: Boolean): JSONObject {
        val roots = snapshot()
        var hit: SNode? = null
        if (text != null || id != null) {
            hit = Screen.find(roots, text, null, id, contains).firstOrNull() ?: return notFound(roots, text, null, id, 0, 0)
        }
        val target = Screen.scrollTarget(roots, hit)
            ?: return JSONObject().put("ok", false).put("scrolled", false).put("error", "no scrollable node")
        val ok = act(target, if (forward) AccessibilityNodeInfo.ACTION_SCROLL_FORWARD else AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD)
        return JSONObject().put("ok", true).put("scrolled", ok).put("target", Screen.describe(target))
    }

    private fun typeInto(label: String?, id: String?, contains: Boolean, index: Int, text: String,
                         append: Boolean, submit: Boolean): JSONObject {
        val roots = snapshot()
        val hits = Screen.findFields(roots, label, id, contains)
        val out = JSONObject()
        val (pick, why) = Screen.chooseField(hits, index)
        if (pick == null) {
            val names = JSONArray()
            (if (hits.size > 1) hits else Screen.fields(roots)).forEach {
                names.put(JSONObject().put("label", Screen.fieldName(it)).put("bounds", JSONArray(it.bounds.toList())))
            }
            val err = if (hits.isEmpty() && label == null && id == null) {
                if (names.length() == 0) "no text field on screen" else "no focused field; name one with label or id"
            } else why
            return out.put("ok", false).put("error", err).put("matches", hits.size).put("fields", names)
        }
        val f = hits[pick]
        val now = Screen.newText(f, text, append)
            ?: return out.put("ok", false).put("error", "can't append to a password field (Android does not show its text); type it whole")
        val ok = setText(f, now)
        out.put("ok", ok).put("matches", hits.size)
            .put("field", JSONObject().apply {
                put("label", Screen.fieldName(f))
                f.id?.let { put("id", it) }
                if (f.password) put("password", true)
                put("bounds", JSONArray(f.bounds.toList()))
            })
            .put("chars", text.length)
        if (!ok) out.put("error", "the field refused the text")
        if (ok && submit) {
            if (Build.VERSION.SDK_INT >= 30) {
                val sent = act(f, AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.id)
                out.put("submitted", sent)
                if (!sent) out.put("note", "the field has no enter action; ui.click its send button")
            } else {
                out.put("submitted", false).put("note", "submit needs Android 11+; ui.click the send button")
            }
        }
        return out
    }

    private fun typeIntoFocus(text: String): Boolean {
        val focus = findFocus(AccessibilityNodeInfo.FOCUS_INPUT)?.takeIf { it.isEditable }
        if (focus != null) {
            return focus.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, Bundle().apply {
                putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
            })
        }
        val field = Screen.fields(snapshot()).firstOrNull { it.focused } ?: return false
        return act(field, AccessibilityNodeInfo.ACTION_SET_TEXT, Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        })
    }

    /** Press [pin]'s keys on the lock screen's PIN pad, one by one (not its enter key). Never says the digits. */
    private fun typePin(pin: String): JSONObject {
        val keys = Screen.pinKeys(snapshot())
        val out = JSONObject().put("digits", pin.length)
        if (pin.any { it !in keys }) return out.put("ok", false).put("keys", keys.size)
        var typed = 0
        for (d in pin) if (press(keys.getValue(d))) typed++
        return out.put("taps_ok", typed).put("ok", typed == pin.length)
    }

    /** Click a key: its clickable self or parent, else a tap on its centre. */
    private fun press(key: SNode): Boolean {
        val target = Screen.clickTarget(key, false)
        return if (target != null) act(target, AccessibilityNodeInfo.ACTION_CLICK)
               else tapAt(key.centerX().toFloat(), key.centerY().toFloat(), TAP_MS)
    }

    // ---- screenshots (Android 11 and later) ----

    @android.annotation.TargetApi(30)
    private fun takeJpeg(quality: Int): ByteArray {
        val done = CountDownLatch(1)
        var picture: Bitmap? = null
        var failed = 0
        takeScreenshot(Display.DEFAULT_DISPLAY, mainExecutor, object : TakeScreenshotCallback {
            override fun onSuccess(shot: ScreenshotResult) {
                val buffer = shot.hardwareBuffer
                picture = Bitmap.wrapHardwareBuffer(buffer, shot.colorSpace)?.copy(Bitmap.Config.ARGB_8888, false)
                buffer.close()
                done.countDown()
            }
            override fun onFailure(code: Int) { failed = code; done.countDown() }
        })
        done.await()
        val bmp = picture ?: throw IllegalStateException(
            "Android refused the screenshot: ${SHOT_ERRORS[failed] ?: "it gave no picture"} (AccessibilityService error $failed)")
        return ByteArrayOutputStream().also { bmp.compress(Bitmap.CompressFormat.JPEG, quality, it) }.toByteArray()
    }

    companion object {
        @Volatile private var instance: Hands? = null
        private val events = Object()
        @Volatile private var count = 0L
        @Volatile private var lastChange = 0L

        /** How long a finger rests for a tap and for a long press (0.5's: 50 ms and 800 ms). */
        const val TAP_MS = 50L
        const val LONG_PRESS_MS = 800L
        /** What "settled" means: nothing on screen has changed for this long (0.5's SETTLE_MS). */
        const val SETTLE_MS = 500L

        private val SHOT_ERRORS = mapOf(
            1 to "an internal error",
            2 to "the accessibility service may not take screenshots",
            3 to "screenshots were asked for too often (Android allows one every third of a second or so)",
            4 to "the display is not there",
            5 to "the window is not there",
            6 to "a secure window is on screen (an app that blocks screenshots)",
        )

        private fun changed() = synchronized(events) {
            count++
            lastChange = SystemClock.uptimeMillis()
            events.notifyAll()
        }

        private fun svc(): Hands = instance ?: throw IllegalStateException(Bridge.HANDS_OFF)

        @JvmStatic fun on(): Boolean = instance != null

        /** The accessibility service's own context, for an overlay that keeps the screen on (Awake). */
        internal fun context(): android.content.Context? = instance

        @JvmStatic fun now(): Long = SystemClock.uptimeMillis()

        /** How many times the screen has changed since the app started. */
        @JvmStatic fun changes(): Long = count

        /** Milliseconds since the screen last changed. */
        @JvmStatic fun quietMs(): Long = SystemClock.uptimeMillis() - lastChange

        /** Wait until the screen changes after [seen] (a count from changes()), or [ms] pass (ms below 1: no end). The count now. */
        @JvmStatic fun awaitChange(seen: Long, ms: Long): Long = synchronized(events) {
            val end = SystemClock.uptimeMillis() + ms
            while (count == seen) {
                if (ms < 1) events.wait()
                else {
                    val left = end - SystemClock.uptimeMillis()
                    if (left <= 0) break
                    events.wait(left)
                }
            }
            count
        }

        /** The screen, compactly; maxItems and maxText below 0 mean every one. */
        @JvmStatic fun screen(maxItems: Int, maxText: Int): String = svc().screen(maxItems, maxText).toString()

        @JvmStatic fun rev(): String = Screen.rev(svc().snapshot())

        /** {package, app} of the app in front, or {} while no window is up. */
        @JvmStatic fun front(): String = svc().front().toString()

        @JvmStatic fun shown(): Boolean = svc().rootInActiveWindow != null

        @JvmStatic fun locked(): Boolean = svc().isLocked()

        @JvmStatic fun matchCount(text: String?, desc: String?, id: String?, contains: Boolean): Int =
            Screen.find(svc().snapshot(), text, desc, id, contains).size

        @JvmStatic fun fieldCount(label: String?, id: String?, contains: Boolean): Int =
            Screen.findFields(svc().snapshot(), label, id, contains).size

        @JvmStatic fun click(text: String?, desc: String?, id: String?, contains: Boolean, index: Int, long: Boolean): String =
            svc().click(text, desc, id, contains, index, long).toString()

        @JvmStatic fun scroll(forward: Boolean, text: String?, id: String?, contains: Boolean): String =
            svc().scroll(forward, text, id, contains).toString()

        @JvmStatic fun typeInto(label: String?, id: String?, contains: Boolean, index: Int, text: String,
                                append: Boolean, submit: Boolean): String =
            svc().typeInto(label, id, contains, index, text, append, submit).toString()

        @JvmStatic fun typeFocused(text: String): Boolean = svc().typeIntoFocus(text)

        @JvmStatic fun typePin(pin: String): String = svc().typePin(pin).toString()

        /** Press the PIN pad's enter key (Screen.enterKey): whether there was one and it took the press. */
        @JvmStatic fun pressEnter(): Boolean {
            val h = svc()
            return Screen.enterKey(h.snapshot())?.let { h.press(it) } ?: false
        }

        /** Press the lock screen's lock icon (Screen.unlockIcon), which brings up its PIN pad: whether there was one. */
        @JvmStatic fun pressUnlockIcon(): Boolean {
            val h = svc()
            return Screen.unlockIcon(h.snapshot())?.let { h.press(it) } ?: false
        }

        /** How many characters the PIN pad's field holds now, or -1 with no field (Screen.pinLength). */
        @JvmStatic fun pinLength(): Int = Screen.pinLength(svc().snapshot())

        /** How many of the PIN pad's digit keys are on screen (0 while there is no pad). */
        @JvmStatic fun pinKeys(): Int = Screen.pinKeys(svc().snapshot()).size

        @JvmStatic fun tap(x: Int, y: Int): Boolean = svc().tapAt(x.toFloat(), y.toFloat(), TAP_MS)

        @JvmStatic fun swipe(x1: Int, y1: Int, x2: Int, y2: Int, ms: Long): Boolean =
            svc().gesture(Path().apply { moveTo(x1.toFloat(), y1.toFloat()); lineTo(x2.toFloat(), y2.toFloat()) }, ms)

        /** back, home or recents (Android's global actions). */
        @JvmStatic fun press(key: String): Boolean = svc().performGlobalAction(when (key) {
            "back" -> GLOBAL_ACTION_BACK
            "home" -> GLOBAL_ACTION_HOME
            "recents" -> GLOBAL_ACTION_RECENTS
            else -> throw IllegalArgumentException("no key $key")
        })

        /**
         * After an action done at [since] (now()): once it has settled, the screen with {changed, settled,
         * waited_ms}. With [app] (the package in front before an app was opened) it first waits until
         * another app is in front; then, as for every action, until nothing has changed for SETTLE_MS.
         * There is no end of ours to either wait: a screen that never stops changing is waited on.
         */
        @JvmStatic fun after(rev: String?, app: String?, since: Long, maxItems: Int, maxText: Int): String {
            val start = SystemClock.uptimeMillis()
            if (app != null) {
                while (true) {
                    val seen = count
                    val now = svc().rootInActiveWindow?.packageName?.toString()
                    if (now != null && now != app) break
                    awaitChange(seen, 0)
                }
            }
            while (true) {
                val seen = count
                val quiet = SystemClock.uptimeMillis() - maxOf(lastChange, since)
                if (quiet >= SETTLE_MS && svc().rootInActiveWindow != null) break
                awaitChange(seen, if (quiet >= SETTLE_MS) 0 else SETTLE_MS - quiet)   // no window yet: wait for one
            }
            val out = svc().screen(maxItems, maxText)
            return out.put("changed", rev == null || out.optString("rev") != rev).put("settled", true)
                .put("waited_ms", SystemClock.uptimeMillis() - start).toString()
        }

        /** The screen as a JPEG at [quality] (0 to 100, Android's range), Android 11 and later. */
        @JvmStatic fun screenshot(quality: Int): ByteArray = svc().takeJpeg(quality)
    }
}

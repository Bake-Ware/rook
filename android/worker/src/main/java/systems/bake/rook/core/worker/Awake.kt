package systems.bake.rook.core.worker

import android.app.KeyguardManager
import android.app.admin.DevicePolicyManager
import android.content.Context
import android.graphics.PixelFormat
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import org.json.JSONObject
import java.util.concurrent.CountDownLatch

/**
 * screen.keep_on and screen.status (brief AP5; 0.5's KeepAwake, without its passive mode): keeps the
 * screen on, so it never reaches the lock timeout, for some minutes or until it is let go.
 *
 * Three layers, as 0.5 had them, so a screen-timeout policy cannot turn the screen off underneath:
 *  - a 1x1 see-through window that takes no touches, with FLAG_KEEP_SCREEN_ON: an accessibility
 *    overlay while the accessibility service is on (it shows over the lock screen too), else an app
 *    overlay where Rook may display over other apps;
 *  - a screen wake lock;
 *  - a nudge every 20 seconds that counts as the person's activity (a short screen lock released with
 *    ON_AFTER_RELEASE), which also resets an attentive timeout.
 * The power button still turns the screen off. Nothing here ends the hold but its minutes, when given,
 * or screen.keep_on off.
 */
object Awake {
    private const val NUDGE_MS = 20_000L
    private val main = Handler(Looper.getMainLooper())
    private var held = false
    private var until = 0L                       // elapsedRealtime the hold ends; 0 while held with no end
    private var wakeLock: PowerManager.WakeLock? = null
    private var overlay: View? = null
    private var overlayHost: Context? = null
    private var overlayKind: String? = null
    @Volatile private var lastCap: String? = null
    @Volatile private var lastAt = 0L

    private val expire = Runnable { if (held && until > 0 && SystemClock.elapsedRealtime() >= until) letGo() else refresh() }
    private val nudge = object : Runnable {
        override fun run() {
            if (!held) return
            if (power().isInteractive) userActivity()
            refresh()
        }
    }

    private fun app() = Bridge.context()
    private fun power() = app().getSystemService(PowerManager::class.java)

    private fun <T> onMain(block: () -> T): T {
        if (Looper.myLooper() == Looper.getMainLooper()) return block()
        var out: Result<T>? = null
        val done = CountDownLatch(1)
        main.post { out = runCatching(block); done.countDown() }
        done.await()
        return out!!.getOrThrow()
    }

    /** screen.keep_on on: hold the screen on for [minutes] (0 or less: until let go). The status. */
    @JvmStatic fun hold(minutes: Int): String = onMain {
        held = true
        until = if (minutes > 0) SystemClock.elapsedRealtime() + minutes * 60_000L else 0L
        refresh()
        status()
    }

    /** screen.keep_on off: let go now. The status. */
    @JvmStatic fun release(): String = onMain {
        letGo()
        status()
    }

    @JvmStatic fun status(): String = onMain { statusJson().toString() }

    /** A screen-driving cap ran (screen.status's last_activity). */
    @JvmStatic fun activity(cap: String) {
        lastCap = cap
        lastAt = System.currentTimeMillis()
    }

    /** The accessibility service came or went: move the window to the overlay it allows. */
    internal fun handsChanged() = main.post { if (held) refresh() }

    private fun letGo() {
        held = false
        until = 0L
        main.removeCallbacks(expire)
        main.removeCallbacks(nudge)
        removeOverlay()
        wakeLock?.let { if (it.isHeld) it.release() }
    }

    private fun refresh() {
        main.removeCallbacks(expire)
        main.removeCallbacks(nudge)
        if (!held) return
        val left = if (until > 0) until - SystemClock.elapsedRealtime() else -1L
        if (until > 0 && left <= 0) return letGo()
        @Suppress("DEPRECATION")
        val lock = wakeLock ?: power().newWakeLock(PowerManager.SCREEN_BRIGHT_WAKE_LOCK or PowerManager.ON_AFTER_RELEASE,
            "rook:keep-on").apply { setReferenceCounted(false) }.also { wakeLock = it }
        if (left > 0) lock.acquire(left) else if (!lock.isHeld) lock.acquire()
        ensureOverlay()
        if (left > 0) main.postDelayed(expire, left)
        main.postDelayed(nudge, if (left > 0) minOf(NUDGE_MS, left) else NUDGE_MS)
    }

    /** Releasing a screen lock flagged ON_AFTER_RELEASE counts as the person's activity. */
    @Suppress("DEPRECATION")
    private fun userActivity() {
        try {
            power().newWakeLock(PowerManager.SCREEN_DIM_WAKE_LOCK or PowerManager.ON_AFTER_RELEASE, "rook:keep-on-nudge")
                .apply { acquire(1_000L); release() }
        } catch (_: Throwable) {}
    }

    private fun ensureOverlay() {
        val hands = Hands.context()
        val host: Context? = hands ?: if (Settings.canDrawOverlays(app())) app() else null
        if (overlay != null && overlayHost === host) return
        removeOverlay()
        host ?: return
        val type = when {
            hands != null -> WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY
            Build.VERSION.SDK_INT >= 26 -> WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
            else -> @Suppress("DEPRECATION") WindowManager.LayoutParams.TYPE_PHONE
        }
        val params = WindowManager.LayoutParams(1, 1, type,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
                WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON or WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT).apply { gravity = Gravity.TOP or Gravity.START; title = "rook-keep-on" }
        val view = View(host).apply { keepScreenOn = true }
        try {
            host.getSystemService(WindowManager::class.java).addView(view, params)
            overlay = view
            overlayHost = host
            overlayKind = if (hands != null) "accessibility" else "app_overlay"
        } catch (_: Throwable) {}
    }

    private fun removeOverlay() {
        val view = overlay ?: return
        try { overlayHost?.getSystemService(WindowManager::class.java)?.removeViewImmediate(view) } catch (_: Throwable) {}
        overlay = null
        overlayHost = null
        overlayKind = null
    }

    private fun statusJson(): JSONObject {
        val context = app()
        val now = SystemClock.elapsedRealtime()
        val out = JSONObject()
            .put("on", held)
            .put("mode", if (held) "manual" else JSONObject.NULL)
            .put("until", if (held && until > 0) (System.currentTimeMillis() + (until - now)) / 1000.0 else JSONObject.NULL)
            .put("remaining_s", if (held && until > 0) (until - now) / 1000 else if (held) JSONObject.NULL else 0)
            .put("wake_lock", wakeLock?.isHeld == true)
            .put("overlay", overlayKind ?: JSONObject.NULL)
            .put("screen_on", power().isInteractive)
            .put("locked", context.getSystemService(KeyguardManager::class.java).isKeyguardLocked)
        lastCap?.let { out.put("last_activity", JSONObject().put("cap", it).put("ago_s", (System.currentTimeMillis() - lastAt) / 1000)) }
        try { out.put("screen_off_timeout_ms", Settings.System.getInt(context.contentResolver, Settings.System.SCREEN_OFF_TIMEOUT)) } catch (_: Throwable) {}
        try {
            val max = context.getSystemService(DevicePolicyManager::class.java).getMaximumTimeToLock(null)
            if (max > 0) out.put("policy_max_time_to_lock_ms", max)
        } catch (_: Throwable) {}
        if (held && overlayKind == null)
            out.put("note", "no keep-screen-on window (the accessibility service is off and Rook may not display over other apps); holding a wake lock only")
        return out
    }
}

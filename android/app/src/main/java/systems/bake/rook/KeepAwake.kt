package systems.bake.rook

import android.app.KeyguardManager
import android.content.Context
import android.content.Intent
import android.graphics.PixelFormat
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.provider.Settings
import android.util.Log
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import org.json.JSONObject
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Keeps the screen on (so it never reaches the lock timeout) while an agent is
 * driving the phone: device.keep_awake holds it for N minutes, and passive mode
 * holds it while screen-driving caps (ui.*, hid.*, screenshot.*, ...) keep
 * arriving, releasing after the idle timeout.
 *
 * Three layers, so an MDM screen-timeout policy (which only caps the timeout)
 * can't turn the screen off under us:
 *  - a 1x1 invisible, untouchable window with FLAG_KEEP_SCREEN_ON: an
 *    accessibility overlay when the HID service is on (no extra permission,
 *    shows over the keyguard), else an app overlay with "Display over other apps";
 *  - a SCREEN_BRIGHT wake lock with a timeout just past the hold, so it lapses
 *    by itself even if this process stops managing it;
 *  - a user-activity poke every 20 seconds (a short ON_AFTER_RELEASE screen lock;
 *    releasing it counts as user activity), which also resets attentive timeouts.
 * The power button still turns the screen off.
 *
 * State changes run on the main thread; the @JvmStatic entry points are called
 * from the Python worker thread and return JSON.
 */
object KeepAwake {
    private const val TAG = "RookKeepAwake"
    const val PREF_AUTO = "keep_awake_auto"
    const val PREF_IDLE_MINUTES = "keep_awake_idle_minutes"
    const val DEFAULT_IDLE_MINUTES = 10
    private const val PREF_ENV_AUTO = "keep_awake_env_auto"
    private const val PREF_ENV_IDLE = "keep_awake_env_idle"
    private const val POKE_MS = 20_000L

    private val main = Handler(Looper.getMainLooper())
    private val policy = KeepAwakePolicy()
    private var app: Context? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var overlay: View? = null
    private var overlayKind: String? = null
    private var overlayHost: Context? = null
    private var lastCap: String? = null
    private var lastActivityAt = 0L

    private val expire = Runnable { refresh() }
    private val poke = object : Runnable {
        override fun run() {
            val ctx = app ?: return
            if (!policy.held(now())) return
            if (power(ctx).isInteractive) userActivity(ctx)
            refresh()
        }
    }

    private fun now() = SystemClock.elapsedRealtime()
    private fun prefs(ctx: Context) = ctx.getSharedPreferences("rook", Context.MODE_PRIVATE)
    private fun power(ctx: Context) = ctx.getSystemService(Context.POWER_SERVICE) as PowerManager

    @JvmStatic fun autoEnabled(ctx: Context): Boolean = prefs(ctx).getBoolean(PREF_AUTO, true)
    @JvmStatic fun idleMinutes(ctx: Context): Int =
        KeepAwakePolicy.clampMinutes(prefs(ctx).getInt(PREF_IDLE_MINUTES, DEFAULT_IDLE_MINUTES))

    private fun <T> onMain(block: () -> T): T {
        if (Looper.myLooper() == Looper.getMainLooper()) return block()
        var out: Result<T>? = null
        val done = CountDownLatch(1)
        main.post { out = runCatching(block); done.countDown() }
        if (!done.await(3, TimeUnit.SECONDS)) throw IllegalStateException("main thread busy")
        return out!!.getOrThrow()
    }

    /** device.keep_awake on: hold the screen for [minutes], waking it if off. */
    @JvmStatic fun engage(ctx: Context, minutes: Int): String = onMain {
        app = ctx.applicationContext
        policy.engage(now(), minutes)
        wake(ctx)
        refresh()
        statusJson(ctx)
    }

    /** device.keep_awake off: release everything (passive mode re-engages on the next screen call). */
    @JvmStatic fun release(ctx: Context): String = onMain {
        app = ctx.applicationContext
        policy.release()
        refresh()
        statusJson(ctx)
    }

    /**
     * A screen-driving cap is about to run. With passive mode on, (re)start the
     * idle timer and wake the screen. Reports whether this call started the hold
     * and whether the lock screen is up, so the caller can unlock.
     */
    @JvmStatic fun activity(ctx: Context, cap: String): String = onMain {
        app = ctx.applicationContext
        lastCap = cap
        lastActivityAt = System.currentTimeMillis()
        val out = JSONObject().put("ok", true)
        if (!autoEnabled(ctx)) {
            out.put("auto", false).put("held", policy.held(now()))
        } else {
            val started = policy.activity(now(), idleMinutes(ctx))
            val woke = wake(ctx)
            refresh()
            out.put("auto", true).put("held", true).put("started", started).put("woke", woke)
        }
        out.put("screen_on", power(ctx).isInteractive)
            .put("locked", keyguardLocked(ctx))
            .put("pin_set", UnlockPin.isSet(ctx))
            .put("auto_unlock", UnlockPin.autoUnlockAllowed(ctx))
            .toString()
    }

    @JvmStatic fun status(ctx: Context): String = onMain {
        app = ctx.applicationContext
        statusJson(ctx)
    }

    /** Change the passive-mode setting (null leaves a field as it is). */
    @JvmStatic fun configure(ctx: Context, auto: Boolean?, idleMinutes: Int?): String = onMain {
        app = ctx.applicationContext
        val e = prefs(ctx).edit()
        auto?.let { e.putBoolean(PREF_AUTO, it) }
        idleMinutes?.let { e.putInt(PREF_IDLE_MINUTES, KeepAwakePolicy.clampMinutes(it)) }
        e.apply()
        settingChanged(ctx)
        statusJson(ctx)
    }

    /**
     * Values pushed by worker.config_apply (env ROOK_KEEP_AWAKE_*). Applied only
     * when they differ from the last pushed value, so a later change in the app's
     * Settings screen isn't overwritten at every worker start.
     */
    @JvmStatic fun applyPushed(ctx: Context, auto: String?, idleMinutes: String?) {
        val p = prefs(ctx)
        val e = p.edit()
        var changed = false
        if (auto != null && auto != p.getString(PREF_ENV_AUTO, null)) {
            e.putString(PREF_ENV_AUTO, auto)
            e.putBoolean(PREF_AUTO, auto.trim().lowercase() in setOf("1", "true", "yes", "on"))
            changed = true
        }
        val minutes = idleMinutes?.trim()?.toIntOrNull()
        if (idleMinutes != null && minutes != null && idleMinutes != p.getString(PREF_ENV_IDLE, null)) {
            e.putString(PREF_ENV_IDLE, idleMinutes)
            e.putInt(PREF_IDLE_MINUTES, KeepAwakePolicy.clampMinutes(minutes))
            changed = true
        }
        if (changed) {
            e.apply()
            main.post { settingChanged(ctx) }
        }
    }

    /** The app's Settings screen or a remote change turned passive mode off/changed the timeout. */
    @JvmStatic fun settingChanged(ctx: Context) {
        main.post {
            app = ctx.applicationContext
            if (!autoEnabled(ctx)) policy.clearAuto()
            refresh()
        }
    }

    /** The HID accessibility service (re)connected or went away: move the overlay. */
    @JvmStatic fun accessibilityChanged() { main.post { refresh() } }

    /** Worker service is going away: drop the hold. */
    @JvmStatic fun shutdown() {
        main.post {
            policy.release()
            refresh()
        }
    }

    @JvmStatic fun screenOn(ctx: Context): Boolean = power(ctx).isInteractive

    @JvmStatic fun keyguardLocked(ctx: Context): Boolean =
        (ctx.getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager).isKeyguardLocked

    // ---- main-thread internals ---------------------------------------------

    /** Make the window, wake lock and timers match the policy. */
    private fun refresh() {
        val ctx = app ?: return
        main.removeCallbacks(expire)
        main.removeCallbacks(poke)
        val t = now()
        if (!policy.held(t)) {
            releaseLocks()
            return
        }
        val left = policy.until() - t
        holdWakeLock(ctx, left)
        ensureOverlay(ctx)
        main.postDelayed(expire, left + 50)
        main.postDelayed(poke, minOf(POKE_MS, left))
    }

    @Suppress("DEPRECATION")
    private fun holdWakeLock(ctx: Context, leftMs: Long) {
        val wl = wakeLock ?: power(ctx).newWakeLock(
            PowerManager.SCREEN_BRIGHT_WAKE_LOCK or PowerManager.ON_AFTER_RELEASE, "rook:keepawake"
        ).apply { setReferenceCounted(false) }.also { wakeLock = it }
        // Re-acquiring resets the timeout: it lapses on its own a little after
        // the hold even if nothing here runs again.
        wl.acquire(leftMs + 30_000L)
    }

    /** Turn the screen on if it is off. Returns true if it was off. */
    @Suppress("DEPRECATION")
    private fun wake(ctx: Context): Boolean {
        val pm = power(ctx)
        if (pm.isInteractive) return false
        try {
            pm.newWakeLock(
                PowerManager.SCREEN_BRIGHT_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP or PowerManager.ON_AFTER_RELEASE,
                "rook:keepawake-wake"
            ).acquire(3_000L)
        } catch (t: Throwable) {
            Log.w(TAG, "wake lock wakeup failed", t)
        }
        // Some builds ignore ACQUIRE_CAUSES_WAKEUP: fall back to the wake activity.
        main.postDelayed({
            if (!pm.isInteractive && Settings.canDrawOverlays(ctx)) {
                try {
                    ctx.startActivity(Intent(ctx, WakeActivity::class.java)
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK).putExtra("hold_ms", 800L))
                } catch (t: Throwable) {
                    Log.w(TAG, "wake activity failed", t)
                }
            }
        }, 400)
        return true
    }

    /** Releasing a screen lock flagged ON_AFTER_RELEASE counts as user activity. */
    @Suppress("DEPRECATION")
    private fun userActivity(ctx: Context) {
        try {
            val wl = power(ctx).newWakeLock(
                PowerManager.SCREEN_DIM_WAKE_LOCK or PowerManager.ON_AFTER_RELEASE, "rook:keepawake-poke")
            wl.acquire(1_000L)
            wl.release()
        } catch (t: Throwable) {
            Log.w(TAG, "user-activity poke failed", t)
        }
    }

    private fun ensureOverlay(ctx: Context) {
        val a11y = HidAccessibilityService.overlayContext()
        val wantHost: Context? = a11y ?: if (Settings.canDrawOverlays(ctx)) ctx.applicationContext else null
        if (overlay != null && overlayHost === wantHost) return
        removeOverlay()
        wantHost ?: return
        val type = when {
            a11y != null -> WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.O -> WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
            else -> @Suppress("DEPRECATION") WindowManager.LayoutParams.TYPE_PHONE
        }
        val lp = WindowManager.LayoutParams(
            1, 1, type,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
                WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON or
                WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT
        ).apply { gravity = Gravity.TOP or Gravity.START; title = "rook-keep-awake" }
        val v = View(wantHost).apply { keepScreenOn = true }
        try {
            (wantHost.getSystemService(Context.WINDOW_SERVICE) as WindowManager).addView(v, lp)
            overlay = v
            overlayHost = wantHost
            overlayKind = if (a11y != null) "accessibility" else "app_overlay"
        } catch (t: Throwable) {
            Log.w(TAG, "keep-awake overlay failed", t)
        }
    }

    private fun removeOverlay() {
        val v = overlay ?: return
        try {
            (overlayHost?.getSystemService(Context.WINDOW_SERVICE) as? WindowManager)?.removeViewImmediate(v)
        } catch (_: Throwable) {
            // The window died with its accessibility service; nothing to remove.
        }
        overlay = null
        overlayHost = null
        overlayKind = null
    }

    private fun releaseLocks() {
        removeOverlay()
        wakeLock?.let { if (it.isHeld) it.release() }
    }

    private fun statusJson(ctx: Context): String {
        val t = now()
        val wall = System.currentTimeMillis()
        fun epoch(until: Long) = if (until > t) (wall + (until - t)) / 1000.0 else JSONObject.NULL
        val held = policy.held(t)
        val out = JSONObject()
            .put("ok", true)
            .put("on", held)
            .put("mode", policy.mode(t) ?: JSONObject.NULL)
            .put("until", epoch(policy.until()))
            .put("remaining_s", if (held) (policy.until() - t) / 1000 else 0)
            .put("manual_until", epoch(policy.manualUntil))
            .put("auto_until", epoch(policy.autoUntil))
            .put("wake_lock", wakeLock?.isHeld == true)
            .put("overlay", overlayKind ?: JSONObject.NULL)
            .put("screen_on", power(ctx).isInteractive)
            .put("locked", keyguardLocked(ctx))
            .put("passive", JSONObject().put("enabled", autoEnabled(ctx)).put("idle_minutes", idleMinutes(ctx)))
            .put("unlock_pin_set", UnlockPin.isSet(ctx))
        if (lastActivityAt > 0)
            out.put("last_activity", JSONObject().put("cap", lastCap).put("ago_s", (wall - lastActivityAt) / 1000))
        try {
            out.put("screen_off_timeout_ms", Settings.System.getInt(ctx.contentResolver, Settings.System.SCREEN_OFF_TIMEOUT))
        } catch (_: Throwable) {}
        try {
            val dpm = ctx.getSystemService(Context.DEVICE_POLICY_SERVICE) as android.app.admin.DevicePolicyManager
            val max = dpm.getMaximumTimeToLock(null)
            if (max > 0) out.put("policy_max_time_to_lock_ms", max)
        } catch (_: Throwable) {}
        if (held && overlayKind == null)
            out.put("note", "no keep-screen-on window (turn on the accessibility service or 'Display over other apps'); holding a wake lock only")
        return out.toString()
    }
}

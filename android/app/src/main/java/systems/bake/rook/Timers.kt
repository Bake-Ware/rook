package systems.bake.rook

import android.app.AlarmManager
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioAttributes
import android.media.Ringtone
import android.media.RingtoneManager
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import org.json.JSONObject

/**
 * Client-scheduled timers. The voice server sends `timer` set/cancel events; the phone
 * arms an alarm and rings it itself (notification with sound, on-device speech, a chat
 * line), so a timer works with the voice connection closed and the app in the background.
 * State is kept in SharedPreferences so it survives process death; [BootReceiver] re-arms
 * it after a reboot or an app update.
 */
object Timers {
    private const val TAG = "RookTimers"
    private const val PREFS = "rook_timers"
    private const val KEY = "book"
    /** Silent (vibrate only): the sound is the alarm-stream ringtone [TimerReceiver] plays. */
    const val CHANNEL = "rook_timer_alerts"
    private const val OLD_CHANNEL = "rook_timers"
    /** How long the alarm sound plays at most (it also stops when the spoken line ends). */
    const val RING_MS = 10_000L
    const val ACTION_FIRE = "systems.bake.rook.TIMER_FIRE"
    const val EXTRA_ID = "id"

    /** Without exact-alarm access, ring within this long after the time (plain inexact alarms can be ~75% late). */
    private const val INEXACT_WINDOW_MS = 60_000L

    private val lock = Any()

    private fun <T> edit(ctx: Context, block: (TimerBook) -> T): T = synchronized(lock) {
        val prefs = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val book = TimerBook.load(prefs.getString(KEY, null))
        val out = block(book)
        prefs.edit().putString(KEY, book.toJson()).commit()
        out
    }

    fun list(ctx: Context): List<ActiveTimer> = synchronized(lock) {
        TimerBook.load(ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY, null)).timers()
    }

    /** A `timer` event from the server (main thread). */
    fun handle(ctx: Context, e: TimerEvent) {
        val change = edit(ctx) { it.apply(e) }
        act(ctx, change)
        changed()
    }

    /** The user cancelled a timer in the app: also tells the server (now, or after the next connect). */
    fun cancel(ctx: Context, id: String) {
        act(ctx, edit(ctx) { it.userCancel(id) })
        changed()
        VoiceService.inst?.flushTimerCancels()
    }

    /** Cancels made on the phone that the server has not been told about yet. */
    fun pendingCancels(ctx: Context): List<String> = synchronized(lock) {
        TimerBook.load(ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY, null)).pendingCancels()
    }

    /** [id]'s cancel was handed to an open voice connection. */
    fun cancelSent(ctx: Context, id: String) = edit(ctx) { it.sent(id) }

    /** After reboot, app update, or an exact-alarm permission change. */
    fun rearm(ctx: Context) {
        val changes = edit(ctx) { it.rearm() }
        changes.forEach { act(ctx, it) }
        if (changes.isNotEmpty()) Log.i(TAG, "re-armed ${changes.size} timer(s)")
    }

    private fun act(ctx: Context, c: TimerBook.Change) {
        when (c) {
            is TimerBook.Change.Arm -> arm(ctx, c.timer)
            is TimerBook.Change.RingNow -> ctx.sendBroadcast(fireIntent(ctx, c.timer.id))
            is TimerBook.Change.Disarm -> {
                alarms(ctx)?.cancel(pending(ctx, c.id))
                notifications(ctx)?.cancel(notificationId(c.id))
            }
            TimerBook.Change.None -> {}
        }
    }

    private fun changed() { Handler(Looper.getMainLooper()).post { VoiceBus.listener?.onTimersChanged() } }

    private fun alarms(ctx: Context) = ctx.getSystemService(Context.ALARM_SERVICE) as? AlarmManager
    private fun notifications(ctx: Context) = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager

    private fun fireIntent(ctx: Context, id: String) = Intent(ctx, TimerReceiver::class.java)
        .setAction(ACTION_FIRE)
        // The data URI makes each timer's PendingIntent distinct (filterEquals ignores extras).
        .setData(Uri.parse("rook-timer:" + Uri.encode(id)))
        .putExtra(EXTRA_ID, id)

    private fun pending(ctx: Context, id: String): PendingIntent = PendingIntent.getBroadcast(
        ctx, notificationId(id), fireIntent(ctx, id), PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)

    fun notificationId(id: String) = 0x7100_0000 or (id.hashCode() and 0x00ff_ffff)

    /** Exact when allowed (USE_EXACT_ALARM / SCHEDULE_EXACT_ALARM), otherwise a one-minute window. */
    fun canExact(ctx: Context): Boolean {
        val am = alarms(ctx) ?: return false
        return Build.VERSION.SDK_INT < Build.VERSION_CODES.S || am.canScheduleExactAlarms()
    }

    private fun arm(ctx: Context, t: ActiveTimer) {
        val am = alarms(ctx) ?: return
        val pi = pending(ctx, t.id)
        try {
            if (canExact(ctx)) am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, t.firesAt, pi)
            else am.setWindow(AlarmManager.RTC_WAKEUP, t.firesAt, INEXACT_WINDOW_MS, pi)
        } catch (e: SecurityException) {
            // Exact-alarm access revoked between the check and the call.
            Log.w(TAG, "exact alarm refused, using a ${INEXACT_WINDOW_MS / 1000} s window: $e")
            am.setWindow(AlarmManager.RTC_WAKEUP, t.firesAt, INEXACT_WINDOW_MS, pi)
        }
    }

    // ---- ringing -----------------------------------------------------------

    private fun ensureChannel(ctx: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val nm = notifications(ctx) ?: return
        if (nm.getNotificationChannel(CHANNEL) != null) return
        // Earlier builds used a channel with its own sound; the alarm sound is now played on the alarm stream.
        if (nm.getNotificationChannel(OLD_CHANNEL) != null) nm.deleteNotificationChannel(OLD_CHANNEL)
        val ch = NotificationChannel(CHANNEL, "Timers", NotificationManager.IMPORTANCE_HIGH).apply {
            description = "Timers you set by voice"
            setSound(null, null)
            enableVibration(true)
        }
        nm.createNotificationChannel(ch)
    }

    /** Ring [id] if it is still armed. Returns the text spoken, or null when nothing rang. */
    fun ring(ctx: Context, id: String): ActiveTimer? {
        val t = edit(ctx) { it.fired(id) } ?: return null
        changed()
        ensureChannel(ctx)
        try {
            val n = NotificationCompat.Builder(ctx, CHANNEL)
                .setSmallIcon(android.R.drawable.ic_lock_idle_alarm)
                .setContentTitle(t.doneText)
                .setContentText(if (t.durationS > 0) "${t.title} · ${timerRemaining(t.durationS * 1000, 0)}" else t.title)
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setCategory(NotificationCompat.CATEGORY_ALARM)
                .setDefaults(NotificationCompat.DEFAULT_VIBRATE)
                .setAutoCancel(true)
                .setContentIntent(NotificationNavigation.mainActivity(ctx))
                .build()
            notifications(ctx)?.notify(notificationId(id), n)
        } catch (e: SecurityException) { Log.w(TAG, "timer notification not allowed: $e") }
        if (!notificationsAllowed(ctx))
            Log.w(TAG, "notifications are off (POST_NOTIFICATIONS denied or blocked): timer '${t.title}' rings by sound and speech only")
        Handler(Looper.getMainLooper()).post { VoiceBus.emit { it.onSpoken(t.doneText) } }
        return t
    }

    private fun notificationsAllowed(ctx: Context): Boolean {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ctx.checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return false
        return NotificationManagerCompat.from(ctx).areNotificationsEnabled()
    }

    /**
     * Starts the default alarm sound on the alarm stream, so a timer is heard with media
     * volume at 0 or notifications denied. The caller stops it; null if none could play.
     */
    fun startAlarmSound(ctx: Context): Ringtone? = try {
        val uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
            ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)
        RingtoneManager.getRingtone(ctx, uri)?.apply {
            audioAttributes = AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build()
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) isLooping = true
            play()
        }
    } catch (e: Exception) { Log.w(TAG, "alarm sound failed: $e"); null }
}

/** Alarm target: rings the timer and speaks it, keeping the process alive until speech ends. */
class TimerReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Timers.ACTION_FIRE) return
        val id = intent.getStringExtra(Timers.EXTRA_ID) ?: return
        val ctx = context.applicationContext
        val t = Timers.ring(ctx, id) ?: return
        val result = goAsync()
        val main = Handler(Looper.getMainLooper())
        val started = System.currentTimeMillis()
        val ringtone = Timers.startAlarmSound(ctx)
        val sid = try {
            JSONObject(SpeakBridge.speak(ctx, t.doneText, "", 1f, 1f, false)).optString("id")
        } catch (e: Exception) { Log.w("RookTimers", "speak failed: $e"); "" }
        var spoken = false
        fun poll() {
            val elapsed = System.currentTimeMillis() - started
            spoken = spoken || sid.isEmpty() || try { JSONObject(SpeakBridge.status(sid)).optBoolean("done", true) } catch (_: Exception) { true }
            // The alarm sound stops when the line has been spoken or after RING_MS, whichever is first
            // (the full RING_MS when speech could not start).
            val ringDone = ringtone == null || (spoken && sid.isNotEmpty()) || elapsed > Timers.RING_MS
            if (ringDone) try { ringtone?.stop() } catch (_: Exception) {}
            if (ringDone && (spoken || elapsed > 25_000)) result.finish()
            else main.postDelayed({ poll() }, 250)
        }
        main.postDelayed({ poll() }, 250)
    }
}

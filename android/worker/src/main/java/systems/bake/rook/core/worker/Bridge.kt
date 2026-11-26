package systems.bake.rook.core.worker

import android.Manifest
import android.app.ActivityManager
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Notification
import android.app.PendingIntent
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.provider.Settings
import org.json.JSONObject
import java.util.concurrent.CountDownLatch

/**
 * What the Android caps' worker parts reach Android through (brief AP2). They are Python, pushed by
 * the hub like any plugin's worker part, and call this through Chaquopy:
 *
 *     Bridge = jclass("systems.bake.rook.core.worker.Bridge")
 *     Bridge.context()                  the application context, for system services
 *     Bridge.ask(permission, cap)       null once granted, else what to tell the caller
 *     Bridge.start(intent, cap)         open an activity: null once opened, else why not
 *     Bridge.clipboard(cap)             the clipboard's text, read from a moment in front, as JSON
 *
 * Each waits for the person when it must, as long as that takes, so it is called off the worker's
 * event loop (asyncio.to_thread).
 *
 * Asking: Android shows a permission's prompt only from an activity, and lets an app in the
 * background open one only once it may display over other apps. So a prompt opens at once, over
 * whatever is on screen, where Android allows that (the app is in front, or may display over other
 * apps), and otherwise comes as a notification that opens it. A runtime permission is Android's own
 * dialog; Do Not Disturb access and displaying over other apps are switches in Android's settings, so
 * their page opens and the answer is read when the person comes back. Nothing of ours is in front of
 * either: no dialog, no confirmation.
 */
object Bridge {
    private lateinit var app: Context
    private val waiting = HashMap<String, CountDownLatch>()      // permission (or "clipboard") -> its open request
    private var clip: String? = null

    internal fun attach(context: Context) {
        app = context.applicationContext
    }

    @JvmStatic fun context(): Context = app

    @JvmStatic fun granted(permission: String): Boolean = when (permission) {
        Manifest.permission.SYSTEM_ALERT_WINDOW -> Settings.canDrawOverlays(app)
        Manifest.permission.ACCESS_NOTIFICATION_POLICY ->
            app.getSystemService(NotificationManager::class.java).isNotificationPolicyAccessGranted
        else -> app.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED
    }

    /** Ask for `permission` for `cap` and wait for the person: null once it is granted, else what to tell the caller. */
    @JvmStatic fun ask(permission: String, cap: String): String? {
        if (granted(permission)) return null
        val asked = request(permission, Intent(app, AskActivity::class.java).putExtra(AskActivity.PERMISSION, permission),
            "Rook needs ${label(permission)}", "For $cap. Tap to answer.", app)
            ?: return notShown(label(permission), cap)
        asked.await()
        return if (granted(permission)) null else refused(permission, cap)
    }

    /** Open an activity for `cap`; first, where Android would not show it, ask to display over other apps. */
    @JvmStatic fun start(intent: Intent, cap: String): String? {
        if (!mayOpen()) ask(Manifest.permission.SYSTEM_ALERT_WINDOW, cap)?.let { return it }
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return try {
            app.startActivity(intent)
            null
        } catch (e: ActivityNotFoundException) {
            "no app on this phone handles that"
        }
    }

    /** The clipboard's text as {"text": ...} (null when empty), or {"error": ...}: read by a see-through screen while it is in front. */
    @JvmStatic fun clipboard(cap: String): String {
        if (!mayOpen()) ask(Manifest.permission.SYSTEM_ALERT_WINDOW, cap)?.let { return JSONObject().put("error", it).toString() }
        val read = synchronized(waiting) {
            waiting[CLIPBOARD] ?: CountDownLatch(1).also {
                waiting[CLIPBOARD] = it
                app.startActivity(Intent(app, AskActivity::class.java).putExtra(AskActivity.CLIPBOARD, true)
                    .addFlags(ASK_FLAGS))
            }
        }
        read.await()
        return JSONObject().put("text", clip ?: JSONObject.NULL).toString()
    }

    /** At start, while the app is in front: ask for notifications (Android 13 and later), which every later ask in
     *  the background rides on. The prompt opens from `context` itself when it is an activity, before it goes. */
    internal fun askToNotify(context: Context) {
        attach(context)
        if (Build.VERSION.SDK_INT >= 33 && !granted(Manifest.permission.POST_NOTIFICATIONS)) {
            request(Manifest.permission.POST_NOTIFICATIONS,
                Intent(app, AskActivity::class.java).putExtra(AskActivity.PERMISSION, Manifest.permission.POST_NOTIFICATIONS),
                "Rook needs ${label(Manifest.permission.POST_NOTIFICATIONS)}", "For the worker's notification. Tap to answer.",
                context)
        }
    }

    /** AskActivity's answer to the request for `key` (a permission, or CLIPBOARD with the text it read). */
    internal fun answered(key: String, text: String? = null) {
        if (key == CLIPBOARD) clip = text
        synchronized(waiting) { waiting.remove(key) }?.countDown()
        app.getSystemService(NotificationManager::class.java).cancel(key.hashCode())
    }

    /**
     * Show `intent` for `key`: at once, from `from`, where Android allows it, and as a notification too unless
     * the app may display over other apps, since Android can still drop a screen an app opens while it is
     * leaving the front. The request to wait on, or null if neither could be shown.
     */
    private fun request(key: String, intent: Intent, title: String, text: String, from: Context): CountDownLatch? = synchronized(waiting) {
        waiting[key]?.let { return it }           // asked already: wait for that answer
        val opened = from is android.app.Activity || mayOpen()
        intent.addFlags(ASK_FLAGS)
        if (opened) from.startActivity(intent)
        if (!Settings.canDrawOverlays(app)) {
            if (!granted(Manifest.permission.POST_NOTIFICATIONS) && Build.VERSION.SDK_INT >= 33 && !opened) return null
            val notifications = app.getSystemService(NotificationManager::class.java)
            notifications.createNotificationChannel(NotificationChannel(ASKS, "Rook asks", NotificationManager.IMPORTANCE_HIGH))
            notifications.notify(key.hashCode(), Notification.Builder(app, ASKS)
                .setContentTitle(title).setContentText(text)
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentIntent(PendingIntent.getActivity(app, key.hashCode(), intent,
                    PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT))
                .setAutoCancel(true).build())
        }
        CountDownLatch(1).also { waiting[key] = it }
    }

    /** Whether Android shows an activity this app opens now: it is in front, or may display over other apps. */
    private fun mayOpen(): Boolean {
        if (Build.VERSION.SDK_INT < 29 || Settings.canDrawOverlays(app)) return true
        val me = ActivityManager.RunningAppProcessInfo()
        ActivityManager.getMyMemoryState(me)
        return me.importance <= ActivityManager.RunningAppProcessInfo.IMPORTANCE_FOREGROUND
    }

    private fun label(permission: String) = when (permission) {
        Manifest.permission.SYSTEM_ALERT_WINDOW -> "to display over other apps"
        Manifest.permission.ACCESS_NOTIFICATION_POLICY -> "Do Not Disturb access"
        Manifest.permission.POST_NOTIFICATIONS -> "to show notifications"
        else -> permission.substringAfterLast('.')
    }

    private fun refused(permission: String, cap: String) = when (permission) {
        Manifest.permission.SYSTEM_ALERT_WINDOW ->
            "$cap needs Rook to display over other apps ($permission): Android opens screens for an app in the " +
                "background only with it, and it is off. On the phone, Settings > Apps > Rook > Display over other " +
                "apps, turn it on, then call again."
        Manifest.permission.ACCESS_NOTIFICATION_POLICY ->
            "$cap needs Do Not Disturb access ($permission), and it is off. On the phone, Settings > Apps > " +
                "Special app access > Do Not Disturb access > Rook, turn it on, then call again."
        else ->
            "$cap needs the permission $permission, and it was refused. On the phone, Settings > Apps > Rook > " +
                "Permissions, allow it, then call again."
    }

    private fun notShown(what: String, cap: String) =
        "$cap needs Rook $what, and Android let Rook neither show the request (it is in the background) nor post " +
            "a notification for it (notifications are off for Rook). Open the Rook app on the phone, or allow its " +
            "notifications (Settings > Apps > Rook > Notifications), then call again."

    private const val ASKS = "rook-asks"
    /** Each ask a screen of its own, so one that is still closing never takes the next. */
    private const val ASK_FLAGS = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_MULTIPLE_TASK
    internal const val CLIPBOARD = "clipboard"
}

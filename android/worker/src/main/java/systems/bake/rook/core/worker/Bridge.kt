package systems.bake.rook.core.worker

import android.Manifest
import android.app.ActivityManager
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Notification
import android.app.PendingIntent
import android.content.ActivityNotFoundException
import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageInstaller
import android.content.pm.PackageManager
import android.os.Build
import android.os.PowerManager
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
 * and, for the screen, input and automation caps (brief AP5):
 *
 *     Bridge.hands(cap)                 null once the accessibility service (Hands) is on, else why not
 *     Bridge.wake(cap, dismiss, holdMs) turn the screen on, over the lock screen (WakeActivity): null, else why not
 *     Bridge.wakeEnd()                  let WakeActivity go
 *     Bridge.awake(cap)                 before a screen-driving cap: note it, and turn the screen on if it is off
 *     Bridge.capture(cap)               null once a screen capture session is up (Capture), asking first, else why not
 *
 * Brief AP4 adds one more thing `ask` asks for: notification access ([NOTIFICATION_ACCESS]), the
 * switch on Android's own settings page that lets [NotificationListener] read and act on other apps'
 * notifications and see every app's media session.
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
    private var captureSaid: String? = null                      // why a screen capture session did not start
    private val handsLock = Object()
    @Volatile private var wakeUp: CountDownLatch? = null

    internal fun attach(context: Context) {
        app = context.applicationContext
    }

    @JvmStatic fun context(): Context = app

    @JvmStatic fun granted(permission: String): Boolean = when (permission) {
        Manifest.permission.SYSTEM_ALERT_WINDOW -> Settings.canDrawOverlays(app)
        Manifest.permission.ACCESS_NOTIFICATION_POLICY ->
            app.getSystemService(NotificationManager::class.java).isNotificationPolicyAccessGranted
        NOTIFICATION_ACCESS -> NotificationListener.granted(app)
        ACCESSIBILITY -> handsEnabled()
        else -> app.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED
    }

    /** Ask for `permission` for `cap` and wait for the person: null once it is granted, else what to tell the caller. */
    @JvmStatic fun ask(permission: String, cap: String): String? = askAll(arrayOf(permission), cap)

    /** Ask for several runtime permissions in one Android prompt (Android 12 wants fine and coarse location so),
     *  as `ask` does: null once all are granted, else what to tell the caller about the first that is not. */
    @JvmStatic fun askAll(permissions: Array<String>, cap: String): String? {
        val missing = permissions.filter { !granted(it) }
        if (missing.isEmpty()) return null
        val key = missing.joinToString(",")
        val asked = request(key, Intent(app, AskActivity::class.java).putExtra(AskActivity.PERMISSION, key),
            "Rook needs ${label(missing[0])}", "For $cap. Tap to answer.", app)
            ?: return notShown(label(missing[0]), cap)
        asked.await()
        if (missing.any { granted(it) }) WorkerService.permitted()      // the service may take a type it now can
        return missing.firstOrNull { !granted(it) }?.let { refused(it, cap) }
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

    /**
     * Make sure the accessibility service is on for `cap`: null once it is, else what to tell the caller. Off,
     * Android's page for it opens (as any ask does) and the cap waits for the person; once it is switched on,
     * it waits for Android to start the service.
     */
    @JvmStatic fun hands(cap: String): String? {
        if (Hands.on()) return null
        if (!handsEnabled()) ask(ACCESSIBILITY, cap)?.let { return it }
        synchronized(handsLock) { while (!Hands.on()) handsLock.wait() }
        return null
    }

    /** The service came or went. Come, it answers an ask for it still open, however the person turned it on. */
    internal fun handsChanged() {
        synchronized(handsLock) { handsLock.notifyAll() }
        if (Hands.on()) synchronized(waiting) { waiting.remove(ACCESSIBILITY) }?.countDown()
        Awake.handsChanged()
    }

    /** Whether Rook's accessibility service is switched on in Android's settings. */
    private fun handsEnabled(): Boolean {
        val on = Settings.Secure.getString(app.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES) ?: return false
        val me = ComponentName(app, Hands::class.java)
        return on.split(':').any { ComponentName.unflattenFromString(it) == me }
    }

    /**
     * Whether Android 13 or later greys out Rook's accessibility switch as a restricted setting: it does so for
     * an app installed from a file (a download, or a file manager), which is how this app is installed. True
     * when Android says the app came from a file, false when from elsewhere, null when it does not say.
     */
    private fun fromAFile(): Boolean? {
        if (Build.VERSION.SDK_INT < 33) return false
        return try {
            when (app.packageManager.getInstallSourceInfo(app.packageName).packageSource) {
                PackageInstaller.PACKAGE_SOURCE_DOWNLOADED_FILE, PackageInstaller.PACKAGE_SOURCE_LOCAL_FILE -> true
                PackageInstaller.PACKAGE_SOURCE_UNSPECIFIED -> null
                else -> false
            }
        } catch (e: Exception) { null }
    }

    /**
     * Turn the screen on, over the lock screen, for `cap` (WakeActivity); with `dismiss`, ask Android to
     * dismiss the lock screen too (with a secure lock, that brings up its PIN pad). `holdMs` above 0 keeps
     * WakeActivity that long; else it goes once the screen is on (with `dismiss`, once the lock screen goes,
     * or at wakeEnd). Null once the screen is on, else why not.
     */
    @JvmStatic fun wake(cap: String, dismiss: Boolean, holdMs: Long): String? {
        val up = CountDownLatch(1)
        wakeUp = up
        start(Intent(app, WakeActivity::class.java).putExtra(WakeActivity.DISMISS, dismiss)
            .putExtra(WakeActivity.HOLD_MS, holdMs), cap)?.let { return it }
        up.await()
        screenOn()
        return null
    }

    /** WakeActivity is up (or gone). */
    internal fun woke() {
        wakeUp?.countDown()
    }

    @JvmStatic fun wakeEnd() {
        WakeActivity.current?.let { it.runOnUiThread { it.finish() } }
    }

    /** Before a screen-driving cap: note it (screen.status), and turn the screen on if it is off, where Android lets Rook open a screen without asking. */
    @JvmStatic fun awake(cap: String) {
        Awake.activity(cap)
        if (!app.getSystemService(PowerManager::class.java).isInteractive && mayOpen()) wake(cap, false, 0)
    }

    /** Wait until the display is on. */
    private fun screenOn() {
        val power = app.getSystemService(PowerManager::class.java)
        if (power.isInteractive) return
        val on = CountDownLatch(1)
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(context: Context, intent: Intent) = on.countDown()
        }
        app.registerReceiver(receiver, IntentFilter(Intent.ACTION_SCREEN_ON))
        try {
            if (!power.isInteractive) on.await()
        } finally {
            app.unregisterReceiver(receiver)
        }
    }

    /**
     * Make sure a screen capture session is up for `cap` (Capture): null once it is, else what to tell the
     * caller. With none, Android's own "start recording or casting" prompt comes up (as any ask does) and the
     * cap waits for the person. The session then lasts until it is stopped, so later captures ask nothing.
     */
    @JvmStatic fun capture(cap: String): String? {
        if (Capture.live()) return null
        val asked = request(CAPTURE, Intent(app, AskActivity::class.java).putExtra(AskActivity.CAPTURE, true),
            "Rook asks to capture the screen", "For $cap. Tap to answer.", app)
            ?: return notShown("to capture the screen", cap)
        asked.await()
        val said = synchronized(waiting) { captureSaid.also { captureSaid = null } }
        if (Capture.live()) return null
        return said ?: ("$cap needs the screen capture allowed, and it was not: on the phone, Android's prompt was " +
            "closed without \"Start\". Call again to be asked again, or turn on Rook's accessibility service " +
            "(Settings > Accessibility > Rook), which takes screenshots with no prompt.")
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
        if (key == CAPTURE) synchronized(waiting) { captureSaid = text }
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
        // An app whose accessibility service is bound may open screens from the background (Android's exemption).
        if (Build.VERSION.SDK_INT < 29 || Settings.canDrawOverlays(app) || Hands.on()) return true
        val me = ActivityManager.RunningAppProcessInfo()
        ActivityManager.getMyMemoryState(me)
        return me.importance <= ActivityManager.RunningAppProcessInfo.IMPORTANCE_FOREGROUND
    }

    private fun label(permission: String) = when (permission) {
        Manifest.permission.SYSTEM_ALERT_WINDOW -> "to display over other apps"
        Manifest.permission.ACCESS_NOTIFICATION_POLICY -> "Do Not Disturb access"
        Manifest.permission.POST_NOTIFICATIONS -> "to show notifications"
        NOTIFICATION_ACCESS -> "notification access"
        Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION -> "location"
        Manifest.permission.ACCESS_BACKGROUND_LOCATION -> "location all the time"
        ACCESSIBILITY -> "its accessibility service turned on"
        else -> permission.substringAfterLast('.')
    }

    private fun refused(permission: String, cap: String) = when (permission) {
        Manifest.permission.SYSTEM_ALERT_WINDOW ->
            "$cap needs Rook to display over other apps ($permission): Android opens screens for an app in the " +
                "background only with it, and it is off. On the phone, Settings > Apps > Rook > Display over other " +
                "apps, turn it on, then call again."
        ACCESSIBILITY -> if (fromAFile() == true) {
            "$cap needs Rook's accessibility service, and it is off. Rook was installed from a file, so Android 13 and " +
                "later grey its switch out (\"Restricted setting\") until you allow it: on the phone, Settings > Apps > " +
                "Rook, the three-dot menu at the top right, \"Allow restricted settings\" (Android asks for the phone's " +
                "PIN), then Settings > Accessibility > Rook, turn it on, then call again."
        } else {
            "$cap needs Rook's accessibility service, and it is off. On the phone, Settings > Accessibility > Rook " +
                "(under Downloaded apps), turn it on, then call again. If its switch is greyed out (\"Restricted " +
                "setting\"), first Settings > Apps > Rook, the three-dot menu, \"Allow restricted settings\"."
        }
        Manifest.permission.ACCESS_NOTIFICATION_POLICY ->
            "$cap needs Do Not Disturb access ($permission), and it is off. On the phone, Settings > Apps > " +
                "Special app access > Do Not Disturb access > Rook, turn it on, then call again."
        NOTIFICATION_ACCESS ->
            "$cap needs notification access for Rook, and it is off. On the phone, Settings > Apps > Special app " +
                "access > Notification access (Device & app notifications) > Rook, turn it on, then call again. If " +
                "Android says it is a restricted setting (an app installed from a link), first Settings > Apps > Rook > " +
                "the menu > Allow restricted settings."
        Manifest.permission.ACCESS_BACKGROUND_LOCATION ->
            "$cap needs Rook's location all the time ($permission), to answer with the app closed, and it is not " +
                "given. On the phone, Settings > Apps > Rook > Permissions > Location, choose Allow all the time, " +
                "then call again."
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
    /** What `ask` takes for notification access (brief AP4): the permission Android's notification listeners are bound with. */
    const val NOTIFICATION_ACCESS = "android.permission.BIND_NOTIFICATION_LISTENER_SERVICE"
    internal const val CAPTURE = "capture"
    /** The accessibility service, asked for as a permission is (it is a switch on Android's settings page). */
    internal const val ACCESSIBILITY = "accessibility"
    internal const val HANDS_OFF = "Rook's accessibility service went off"
}

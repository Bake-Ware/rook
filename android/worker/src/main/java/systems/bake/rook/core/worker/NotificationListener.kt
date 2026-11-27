package systems.bake.rook.core.worker

import android.app.ActivityOptions
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.RemoteInput
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import org.json.JSONArray
import org.json.JSONObject

/**
 * The phone's one notification listener (brief AP4): what notification-android reads other apps'
 * notifications through, runs their actions with (a reply typed into a notification's own reply field
 * among them) and dismisses them with, and what media-android names to Android to see every app's
 * media session. Android binds it whenever the person has given Rook notification access, on a page of
 * its own in Settings, which the first cap that needs it opens through [Bridge.ask].
 *
 * Android binds it itself, in the worker's process, so it answers for as long as that process lives:
 * with the app swiped away (the worker's foreground service keeps the process) and, being bound again
 * by Android at boot, after a reboot. If Android unbinds it, it asks to be bound again (requestRebind),
 * and [WorkerService] asks too when it starts.
 *
 * The caps' Python reaches it as `NotificationListener.bound()`, which waits until Android has bound it
 * and gives the listener, whose [list], [get], [dismiss] and [act] answer in JSON.
 */
class NotificationListener : NotificationListenerService() {

    override fun onListenerConnected() {
        synchronized(lock) {
            instance = this
            lock.notifyAll()
        }
    }

    override fun onListenerDisconnected() {
        synchronized(lock) { if (instance === this) instance = null }
        rebind(applicationContext)
    }

    override fun onDestroy() {
        synchronized(lock) { if (instance === this) instance = null }
        super.onDestroy()
    }

    /** Every notification in the shade now, every app's, newest first. */
    fun list(): String {
        val out = JSONArray()
        shown().sortedByDescending { it.postTime }.forEach { out.put(describe(it)) }
        return out.toString()
    }

    /** {notification, actions: [{index, title, takes_text}]} for `key`, or {error}. */
    fun get(key: String): String {
        val sbn = find(key) ?: return missing(key)
        val actions = JSONArray()
        sbn.notification.actions?.forEachIndexed { i, a ->
            actions.put(JSONObject().put("index", i).put("title", a.title?.toString() ?: "")
                .put("takes_text", a.remoteInputs?.isNotEmpty() == true))
        }
        return JSONObject().put("notification", describe(sbn)).put("actions", actions).toString()
    }

    /** Dismiss `key`: {} or {error}. */
    fun dismiss(key: String): String {
        val sbn = find(key) ?: return missing(key)
        if (!sbn.isClearable) {
            return error("Android does not let that notification be dismissed: ${sbn.packageName} keeps it on " +
                "(an ongoing notification). Its app can take it away, or one of its actions (notification.get).")
        }
        cancelNotification(key)
        return "{}"
    }

    /** Run action `index` of `key`, with `text` in its reply field when it has one: {action, app} or {error}. */
    fun act(context: Context, key: String, index: Int, text: String?): String {
        val sbn = find(key) ?: return missing(key)
        val actions = sbn.notification.actions ?: emptyArray()
        val action = actions.getOrNull(index)
            ?: return error("that notification has no action $index; it has ${actions.size} (notification.get lists them)")
        val inputs = action.remoteInputs
        val named = action.title?.toString()?.takeIf { it.isNotBlank() }?.let { "\"$it\"" } ?: "action $index"
        return try {
            if (inputs != null && inputs.isNotEmpty()) {
                if (text.isNullOrEmpty()) return error("$named is a reply: give text")
                val filled = Intent()
                RemoteInput.addResultsToIntent(inputs, filled, Bundle().apply {
                    inputs.forEach { putCharSequence(it.resultKey, text) }
                })
                send(action.actionIntent, context, filled)
            } else {
                if (!text.isNullOrEmpty()) return error("$named takes no text; leave text out")
                send(action.actionIntent, context, null)
            }
            JSONObject().put("action", action.title?.toString() ?: "").put("app", sbn.packageName).toString()
        } catch (e: PendingIntent.CanceledException) {
            error("${sbn.packageName} no longer takes that action (it was cancelled); notification.get shows what it has now")
        }
    }

    /** Send an action's intent; Android 14 and later open an activity it starts only when the sender says so. */
    private fun send(intent: PendingIntent, context: Context, filled: Intent?) {
        val options = if (Build.VERSION.SDK_INT >= 34) {
            ActivityOptions.makeBasic()
                .setPendingIntentBackgroundActivityStartMode(ActivityOptions.MODE_BACKGROUND_ACTIVITY_START_ALLOWED).toBundle()
        } else null
        intent.send(context, 0, filled, null, null, null, options)
    }

    private fun shown(): List<StatusBarNotification> = activeNotifications?.toList() ?: emptyList()

    private fun find(key: String) = shown().firstOrNull { it.key == key }

    private fun missing(key: String) =
        error("no notification has the key $key now: it was dismissed or answered, or the key is wrong. notification.list gives the keys.")

    private fun error(message: String) = JSONObject().put("error", message).toString()

    private fun describe(sbn: StatusBarNotification): JSONObject {
        val extras = sbn.notification.extras
        val title = extras?.getCharSequence("android.title")?.toString()
        val big = extras?.getCharSequence("android.bigText")?.toString()
        val text = extras?.getCharSequence("android.text")?.toString() ?: big
        return JSONObject().put("key", sbn.key).put("package", sbn.packageName)
            .put("title", title ?: JSONObject.NULL).put("text", text ?: JSONObject.NULL)
            .put("ts", sbn.postTime / 1000.0).put("posted_ms", sbn.postTime).put("clearable", sbn.isClearable)
            .apply {
                extras?.getCharSequence("android.subText")?.toString()?.let { put("sub_text", it) }
                if (big != null && big != text) put("big_text", big)
            }
    }

    companion object {
        private val lock = Object()
        private var instance: NotificationListener? = null

        @JvmStatic fun component(context: Context) = ComponentName(context, NotificationListener::class.java)

        /** Whether the person has given Rook notification access. */
        @JvmStatic fun granted(context: Context): Boolean {
            if (Build.VERSION.SDK_INT >= 27) {
                return context.getSystemService(NotificationManager::class.java).isNotificationListenerAccessGranted(component(context))
            }
            val enabled = Settings.Secure.getString(context.contentResolver, "enabled_notification_listeners") ?: return false
            return enabled.split(":").any { ComponentName.unflattenFromString(it) == component(context) }
        }

        /** Ask Android to bind the listener again, when it has access and is not bound. */
        @JvmStatic fun rebind(context: Context) {
            if (synchronized(lock) { instance } == null && granted(context)) {
                requestRebind(component(context))
            }
        }

        /** The listener, once Android has bound it: called after access is granted, it waits for the binding. */
        @JvmStatic fun bound(): NotificationListener {
            rebind(Bridge.context())
            synchronized(lock) {
                while (instance == null) lock.wait()
                return instance!!
            }
        }
    }
}

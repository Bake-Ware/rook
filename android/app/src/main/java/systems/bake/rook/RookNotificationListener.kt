package systems.bake.rook

import android.app.RemoteInput
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import org.json.JSONArray
import org.json.JSONObject
import java.util.ArrayDeque

/**
 * Reads every posted notification (title, text, app, actions) into a small ring
 * buffer that the Python `notify.*` plugin snapshots. Needs the "Notification
 * access" special grant (Settings → Notifications → Device & app notifications),
 * enabled from the app like the accessibility service.
 *
 * Catches message previews from every app that notifies — SMS, WhatsApp, Signal,
 * email, etc. — not just SMS.
 */
class RookNotificationListener : NotificationListenerService() {

    override fun onListenerConnected() {
        connected = true
        instance = this
        // Seed with whatever is already in the shade.
        try {
            activeNotifications?.forEach { remember(it) }
        } catch (_: Throwable) {}
    }

    override fun onListenerDisconnected() {
        connected = false
        if (instance === this) instance = null
    }

    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        sbn ?: return
        remember(sbn)
        // 0.4 attention: classify on the phone (AttentionStore); only urgent/family summaries leave it.
        // 0.5 has no attention logic on the phone (castling-attention): the notification-android
        // plugin hands notifications to the hub, where the house agent's attention pass reads them.
        if (Castling.on(applicationContext)) return
        try { AttentionStore.observe(applicationContext, sbn) } catch (_: Throwable) {}
    }

    private fun remember(sbn: StatusBarNotification) {
        val n = sbn.notification ?: return
        val ex = n.extras
        val title = ex?.getCharSequence("android.title")?.toString()
        val text = ex?.getCharSequence("android.text")?.toString()
            ?: ex?.getCharSequence("android.bigText")?.toString()
        val o = JSONObject()
        o.put("key", sbn.key)
        o.put("package", sbn.packageName)
        o.put("title", title ?: JSONObject.NULL)
        o.put("text", text ?: JSONObject.NULL)
        o.put("ts", sbn.postTime / 1000.0)
        o.put("posted_ms", sbn.postTime)
        // Mail apps: subText is usually the account, bigText the subject plus a preview.
        ex?.getCharSequence("android.subText")?.toString()?.let { o.put("sub_text", it) }
        ex?.getCharSequence("android.bigText")?.toString()?.takeIf { it != text }?.let { o.put("big_text", it.take(1000)) }
        o.put("clearable", sbn.isClearable)
        synchronized(ring) {
            ring.addLast(o)
            while (ring.size > MAX) ring.removeFirst()
        }
    }

    companion object {
        private const val MAX = 200
        private val ring = ArrayDeque<JSONObject>()
        @Volatile private var connected = false
        @Volatile private var instance: RookNotificationListener? = null

        /** Dismiss a notification by its key. Returns false if we're not connected. */
        @JvmStatic
        fun dismiss(key: String): Boolean {
            val svc = instance ?: return false
            return try { svc.cancelNotification(key); true } catch (_: Throwable) { false }
        }

        private fun active(key: String): StatusBarNotification? =
            try { instance?.activeNotifications?.firstOrNull { it.key == key } } catch (_: Throwable) { null }

        /** The notification's own actions (Reply, Mark as read, ...): index, title, whether it takes text. */
        @JvmStatic
        fun actionsJson(key: String): String {
            if (instance == null) return JSONObject().put("ok", false).put("error", "notification access is not connected").toString()
            val sbn = active(key) ?: return JSONObject().put("ok", false).put("error", "no such notification (gone?)").toString()
            val arr = JSONArray()
            sbn.notification.actions?.forEachIndexed { i, a ->
                arr.put(JSONObject().put("index", i).put("title", a.title?.toString() ?: "")
                    .put("takes_text", a.remoteInputs?.isNotEmpty() == true))
            }
            return JSONObject().put("ok", true).put("key", key).put("app", sbn.packageName).put("actions", arr).toString()
        }

        /** Run one of the notification's own actions; [text] fills its reply field when it has one. */
        @JvmStatic
        fun act(ctx: Context, key: String, index: Int, text: String?): String {
            fun err(e: String) = JSONObject().put("ok", false).put("error", e).toString()
            if (instance == null) return err("notification access is not connected")
            val sbn = active(key) ?: return err("no such notification (gone?)")
            val action = sbn.notification.actions?.getOrNull(index) ?: return err("no action $index on that notification")
            return try {
                val inputs = action.remoteInputs
                if (inputs != null && inputs.isNotEmpty()) {
                    if (text.isNullOrBlank()) return err("\"${action.title}\" needs text")
                    val intent = Intent()
                    val results = Bundle()
                    for (ri in inputs) results.putCharSequence(ri.resultKey, text)
                    RemoteInput.addResultsToIntent(inputs, intent, results)
                    action.actionIntent.send(ctx, 0, intent)
                } else {
                    if (!text.isNullOrBlank()) return err("\"${action.title}\" takes no text")
                    action.actionIntent.send()
                }
                JSONObject().put("ok", true).put("action", action.title?.toString() ?: "").put("app", sbn.packageName).toString()
            } catch (e: Throwable) { err("${e.javaClass.simpleName}: ${e.message}") }
        }

        /** Newest-first JSON array of recent notifications. */
        @JvmStatic
        fun snapshotJson(limit: Int): String {
            val arr = JSONArray()
            synchronized(ring) {
                val list = ring.toList().asReversed()
                for ((i, o) in list.withIndex()) {
                    if (i >= limit) break
                    arr.put(o)
                }
            }
            return arr.toString()
        }

        @JvmStatic
        fun isConnected(): Boolean = connected

        /** Whether the user has granted notification access to this app. */
        @JvmStatic
        fun isEnabled(ctx: Context): Boolean {
            return try {
                val flat = Settings.Secure.getString(
                    ctx.contentResolver, "enabled_notification_listeners"
                ) ?: return false
                val me = ComponentName(ctx, RookNotificationListener::class.java)
                flat.split(":").any {
                    val cn = ComponentName.unflattenFromString(it)
                    cn != null && cn.packageName == me.packageName
                }
            } catch (_: Throwable) { false }
        }
    }
}

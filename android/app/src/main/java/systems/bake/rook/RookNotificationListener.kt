package systems.bake.rook

import android.content.ComponentName
import android.content.Context
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

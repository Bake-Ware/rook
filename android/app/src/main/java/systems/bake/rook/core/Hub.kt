package systems.bake.rook.core

import android.content.Context
import android.webkit.CookieManager
import java.net.HttpURLConnection
import java.net.URI
import java.net.URL
import org.json.JSONObject
import systems.bake.rook.core.worker.RookWorker

/** The hub this app opens, and joining the phone to it as a worker (brief AP1). */
object Hub {
    private const val PREFS = "rook"

    /**
     * A hub's origin (scheme, host and port) from what was scanned or typed: its address, the
     * /app page's QR code (<hub>/app/rook.apk), or a bare host, which is taken as https. Null for
     * something that is not a web address.
     */
    fun origin(text: String): String? {
        var t = text.trim()
        if (t.isEmpty()) return null
        if (!t.contains("://")) t = "https://$t"
        val uri = try { URI(t) } catch (e: Exception) { return null }
        val scheme = uri.scheme?.lowercase()
        if (scheme != "http" && scheme != "https") return null
        val host = uri.host?.lowercase() ?: return null
        return "$scheme://$host" + if (uri.port != -1) ":${uri.port}" else ""
    }

    fun saved(context: Context): String? = prefs(context).getString("hub", null)

    fun save(context: Context, hub: String) = prefs(context).edit().putString("hub", hub).apply()

    fun joined(context: Context): Boolean = prefs(context).getBoolean("joined", false)

    /** On every launch and after boot, once the phone has joined: the worker, with no code. */
    fun startWorker(context: Context) {
        val hub = saved(context) ?: return
        if (joined(context)) RookWorker.start(context.applicationContext, hub, null)
    }

    /**
     * The first time only, once the person is signed in: a join token minted with their session
     * (token.create, purpose join, through /api/call with the web view's cookie), and the worker
     * started with it. False while nobody is signed in (the hub answers 401). Runs off the main thread.
     */
    fun join(context: Context, hub: String): Boolean {
        val cookie = CookieManager.getInstance().getCookie(hub) ?: return false
        val body = JSONObject()
            .put("worker", "hub").put("cap", "token.create")
            .put("args", JSONObject().put("purpose", "join"))
            .put("why", "The Rook app on this phone joins it to the hub as a worker.")
        val http = URL("$hub/api/call").openConnection() as HttpURLConnection
        try {
            http.requestMethod = "POST"
            http.doOutput = true
            http.setRequestProperty("Content-Type", "application/json")
            http.setRequestProperty("Cookie", cookie)
            http.outputStream.use { it.write(body.toString().toByteArray()) }
            if (http.responseCode != 200) return false
            val reply = JSONObject(http.inputStream.bufferedReader().readText())
            val token = reply.getJSONObject("result").getString("token")
            prefs(context).edit().putBoolean("joined", true).apply()
            RookWorker.start(context.applicationContext, hub, token)
            return true
        } finally {
            http.disconnect()
        }
    }

    private fun prefs(context: Context) = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
}

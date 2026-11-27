package systems.bake.rook.core

import android.content.Context
import android.webkit.CookieManager
import java.net.HttpURLConnection
import java.net.URI
import java.net.URL
import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import org.json.JSONObject
import systems.bake.rook.core.worker.RookWorker
import systems.bake.rook.core.worker.WorkerState

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

    /**
     * Whether the phone has joined: its worker has linked to the hub at least once (brief AP6). A join
     * code alone is not enough: had the hub refused it, or had the worker never used it, a launch
     * that took the phone for joined would start the worker with that code again for good. Until the
     * worker links, each launch mints a fresh code once the person is signed in.
     */
    fun joined(context: Context): Boolean = prefs(context).getBoolean("joined", false)

    private val minted = AtomicBoolean(false)       // a code was minted in this process: one is enough
    private val watching = AtomicBoolean(false)

    /** Once per process: the first time the worker says it is linked, the phone has joined. */
    fun watch(context: Context) {
        if (!watching.compareAndSet(false, true)) return
        val app = context.applicationContext
        CoroutineScope(SupervisorJob() + Dispatchers.Default).launch {
            RookWorker.state.collect { if (it is WorkerState.Linked) markJoined(app) }
        }
    }

    private fun markJoined(context: Context) = prefs(context).edit().putBoolean("joined", true).apply()

    /** On every launch (and, by the worker's own receiver, after boot), once the phone has joined: the worker, with no code. */
    fun startWorker(context: Context) {
        val hub = saved(context) ?: return
        if (joined(context)) RookWorker.start(context.applicationContext, hub, null)
    }

    /**
     * Until the phone has joined, once the person is signed in: a join token minted with their session
     * (token.create, purpose join, through /api/call with the web view's cookie), and the worker
     * started with it, once in this process. False while nobody is signed in (the hub answers 401) or
     * the hub cannot be reached: the next request to the hub tries again. Runs off the main thread.
     */
    fun join(context: Context, hub: String): Boolean {
        if (RookWorker.state.value is WorkerState.Linked) {     // it linked while nothing here was watching
            markJoined(context)
            return true
        }
        if (minted.get()) return false
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
            minted.set(true)
            RookWorker.start(context.applicationContext, hub, token)
            return true
        } finally {
            http.disconnect()
        }
    }

    private fun prefs(context: Context) = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
}

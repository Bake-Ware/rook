package systems.bake.rook

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.net.URI
import java.util.concurrent.TimeUnit

/** A Rook 0.5 hub cap, called as the person. [WatchSpeech] takes this so tests can fake the hub. */
fun interface HubCaps {
    /** The cap's result. Throws [HubDoor.Refused] when the hub answers no, IOException when it can't be reached. */
    fun call(cap: String, args: JSONObject, why: String, timeoutMs: Long): JSONObject
}

/**
 * The 0.5 hub's HTTP door (`POST /call`, rook/core2/hub/doors/http.py) with the person's own
 * token: every call runs as them, with a why, and is journaled. Hub caps live on worker `rook`.
 * Plain http only to this phone itself (`adb reverse` in testing): the token is the person's.
 */
class HubDoor(base: String, private val token: String, client: OkHttpClient = OkHttpClient()) : HubCaps {
    class Refused(message: String) : Exception(message)

    private val url = check(base) + "/call"
    private val http = client

    override fun call(cap: String, args: JSONObject, why: String, timeoutMs: Long): JSONObject {
        val body = JSONObject().put("worker", HUB).put("cap", cap).put("args", args).put("why", why)
        val req = Request.Builder().url(url)
            .header("Authorization", "Bearer $token")
            .post(body.toString().toRequestBody(JSON))
            .build()
        val reply = http.newBuilder().callTimeout(timeoutMs, TimeUnit.MILLISECONDS)
            .readTimeout(timeoutMs, TimeUnit.MILLISECONDS).build()
            .newCall(req).execute().use { r ->
                val text = r.body?.string() ?: ""
                if (r.code == 401) throw Refused("the hub doesn't know this token (Settings > Band)")
                if (!r.isSuccessful) throw IOException("hub answered HTTP ${r.code}")
                try { JSONObject(text) } catch (e: Exception) { throw IOException("hub sent no JSON") }
            }
        if (reply.optBoolean("ok")) return reply.optJSONObject("result") ?: JSONObject()
        if (reply.optBoolean("held")) throw Refused("waiting for your approval (hold ${reply.optString("hold")})")
        throw Refused(reply.optString("error", "the hub refused $cap"))
    }

    companion object {
        const val HUB = "rook"
        private val JSON = "application/json".toMediaType()
        private val LOCAL = setOf("127.0.0.1", "localhost", "::1", "[::1]")

        /** The door's base address without a trailing slash; throws IllegalArgumentException. */
        fun check(base: String): String {
            val u = try { URI(base.trim()) } catch (e: Exception) { throw IllegalArgumentException("bad hub address: $base") }
            val scheme = u.scheme?.lowercase()
            require(u.host != null && (scheme == "https" || (scheme == "http" && u.host.lowercase() in LOCAL))) {
                "the hub address must be https:// (got $base)"
            }
            return base.trim().trimEnd('/')
        }
    }
}

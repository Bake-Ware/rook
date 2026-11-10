package systems.bake.rook

import android.content.Context
import android.net.Uri
import com.chaquo.python.PyObject
import org.json.JSONObject

/**
 * The phone's worker (`castling_entry`): the phone signs in to its home hub
 * (the QR, or one tap on the hub's Workers page) and the hub's config bundle
 * gives it its address and keys. The watch talks to the hub's `speech`
 * plugin ([WatchSpeech], with the person's hub token); the phone's own voice
 * and the widgets talk to the voice server.
 *
 * Every call into Python here blocks: call it off the main thread.
 */
object Castling {
    /** The name the 0.5 hub gave this phone (the notification shows it). */
    const val NAME_PREF = "rook05_name"
    /** The person's token for the hub's door (`POST /call` as them): the watch's voice uses it. */
    const val TOKEN_PREF = "rook05_token"

    private fun entry(ctx: Context): PyObject =
        PythonHost.ensureStarted(ctx).getModule("castling_entry")

    private fun filesDir(ctx: Context) = ctx.applicationContext.filesDir.absolutePath

    /** `{enrolled, name, worker_id, hub, url, owner}`. */
    fun status(ctx: Context): JSONObject =
        JSONObject(entry(ctx).callAttr("status", filesDir(ctx)).toString())

    /** Python calls [say] while it waits for someone to let the phone in. */
    interface Listener { fun say(text: String) }

    /** Sign in with [target] (the hub's address, or its QR's text); `{ok, error?, ...status}`. */
    fun enroll(ctx: Context, target: String, name: String, force: Boolean, listener: Listener): JSONObject =
        JSONObject(entry(ctx).callAttr("enroll", filesDir(ctx), target, name, listener, force).toString())

    fun cancel(ctx: Context) { entry(ctx).callAttr("cancel") }

    /** A `rook://enroll?url=…&hub=…` link (the sign-in QR) -> (url, hub id), else null. */
    fun enrollLink(uri: Uri?): Pair<String, String?>? {
        if (uri == null || uri.scheme != "rook" || uri.host != "enroll") return null
        val url = uri.getQueryParameter("url") ?: return null
        return url to uri.getQueryParameter("hub")
    }
}

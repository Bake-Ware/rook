package systems.bake.rook

import android.content.Context
import android.net.Uri
import com.chaquo.python.PyObject
import org.json.JSONObject

/**
 * The 0.4/0.5 switch and the Rook 0.5 (Castling) side of the app.
 *
 * 0.4 is the default: `worker_entry` with a hub address and band key from
 * pairing or Google sign-in. 0.5 runs `castling_entry`: the phone signs in to
 * its home hub (the QR, or a code the person approves on the dashboard) and
 * the hub's config bundle replaces the hand-copied address and keys. Voice,
 * the watch and the widgets still talk to the 0.4 voice server either way.
 *
 * Every call into Python here blocks: call it off the main thread.
 */
object Castling {
    const val PREF = "rook_core"
    const val V04 = "0.4"
    const val V05 = "0.5"
    /** The name the 0.5 hub gave this phone (the notification shows it). */
    const val NAME_PREF = "rook05_name"

    fun version(ctx: Context): String =
        ctx.getSharedPreferences("rook", Context.MODE_PRIVATE).getString(PREF, V04) ?: V04

    fun on(ctx: Context) = version(ctx) == V05

    fun setVersion(ctx: Context, v: String) {
        ctx.getSharedPreferences("rook", Context.MODE_PRIVATE).edit().putString(PREF, v).apply()
    }

    private fun entry(ctx: Context): PyObject =
        PythonHost.ensureStarted(ctx).getModule("castling_entry")

    private fun filesDir(ctx: Context) = ctx.applicationContext.filesDir.absolutePath

    /** `{enrolled, name, worker_id, hub, url, owner}`. */
    fun status(ctx: Context): JSONObject =
        JSONObject(entry(ctx).callAttr("status", filesDir(ctx)).toString())

    /** Python calls [say] with the code to approve while it waits. */
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

package systems.bake.rook

import android.content.Context
import android.net.Uri
import android.util.Log
import com.chaquo.python.PyObject
import org.json.JSONObject
import java.io.File

/**
 * The 0.4/0.5 switch and the Rook 0.5 (Castling) side of the app.
 *
 * 0.4 is the default: `worker_entry` with a hub address and band key from
 * pairing or Google sign-in. 0.5 runs `castling_entry`: the phone signs in to
 * its home hub (the QR, or a code the person approves on the dashboard) and
 * the hub's config bundle replaces the hand-copied address and keys. The
 * watch talks to the hub's `speech` plugin in 0.5 ([WatchSpeech], with the
 * person's hub token); the phone's own voice and the widgets still talk to the
 * 0.4 voice server either way.
 *
 * Every call into Python here blocks: call it off the main thread.
 */
object Castling {
    const val PREF = "rook_core"
    const val V04 = "0.4"
    const val V05 = "0.5"
    /** The name the 0.5 hub gave this phone (the notification shows it). */
    const val NAME_PREF = "rook05_name"
    /** The person's token for the hub's door (`POST /call` as them): the watch's voice uses it. */
    const val TOKEN_PREF = "rook05_token"

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

    /** True while the 0.5 worker has a live link to its hub. */
    fun connected(ctx: Context): Boolean = entry(ctx).callAttr("connected").toBoolean()

    // ---- the switch asked from afar ---------------------------------------
    // `files/rook05/switch.json` is a one-shot request to run the other Rook:
    // `{"to": "0.5", "hub": "<address>", "name": "<name>", "trial_seconds": 300}`
    // (written by 0.4's shell.exec) or `{"to": "0.4"}` (0.5's phone.switch).
    // WorkerService takes it and writes how it went to switch-result.json.

    fun switchFile(ctx: Context) = File(ctx.applicationContext.filesDir, "rook05/switch.json")

    /** The pending request, removed so it runs once; null when there is none or it isn't JSON. */
    fun takeSwitch(ctx: Context): JSONObject? {
        val f = switchFile(ctx)
        if (!f.isFile) return null
        val text = runCatching { f.readText() }.getOrNull()
        f.delete()
        return runCatching { JSONObject(text ?: "") }.getOrElse {
            switchResult(ctx, false, "switch.json isn't a JSON object"); null
        }
    }

    /** [ok] null: still working (e.g. a code waits for approval). */
    fun switchResult(ctx: Context, ok: Boolean?, text: String) {
        Log.i("RookCastling", "switch: $text")
        runCatching {
            val f = File(ctx.applicationContext.filesDir, "rook05/switch-result.json")
            f.parentFile?.mkdirs()
            val tmp = File(f.path + ".tmp")
            tmp.writeText(JSONObject().put("ok", ok ?: JSONObject.NULL).put("text", text)
                .put("version", version(ctx)).put("at", System.currentTimeMillis() / 1000).toString())
            tmp.renameTo(f)
        }
    }

    /** A `rook://enroll?url=…&hub=…` link (the sign-in QR) -> (url, hub id), else null. */
    fun enrollLink(uri: Uri?): Pair<String, String?>? {
        if (uri == null || uri.scheme != "rook" || uri.host != "enroll") return null
        val url = uri.getQueryParameter("url") ?: return null
        return url to uri.getQueryParameter("hub")
    }
}

package systems.bake.rook.core.worker

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.database.ContentObserver
import android.net.Uri
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import com.chaquo.python.Python
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.RandomAccessFile
import java.util.concurrent.Executors

/**
 * What the phone tells the hub by itself (brief AP7), as events on the hub's bus (J2):
 *
 * - `sms.received` {from, body, ts}: a text came into the inbox ([SmsWatch]).
 * - `notification-android.received` {key, package, title, text, ts}: another app posted a notification
 *   ([NotificationListener.onNotificationPosted]).
 * - `notification-android.card` {id, type, decision, choice, reason, ts}: what the person did with a card ([Cards]).
 *
 * Every one goes the same way: [tell] writes it at the end of one file in [Storage] (readable before the
 * phone's first unlock after a restart, brief AP8, so one made then is kept too), and a
 * thread of its own hands the file's events, oldest first, to the running worker (rook_android.emit -> the
 * worker's own emit). One the worker cannot take yet (not linked) stays, with every one after it, and goes
 * at the next link ([WorkerService] calls [flush] whenever the worker links). Nothing is dropped for being old
 * or many, and nothing is cut.
 *
 * Everything this keeps is under [dir]: the unsent events and what was last told of the inbox and of the
 * notifications, so a restart or a reboot tells nothing twice.
 */
object Events {
    private const val TAG = "RookEvents"
    private val sender = Executors.newSingleThreadExecutor { Thread(it, "rook-events").apply { isDaemon = true } }

    /** Where every event and every "last told" is kept. One place, so it can move as one (AP8). */
    @JvmStatic fun dir(context: Context): File = File(Storage.files(context), "events").apply { mkdirs() }

    /** Event `name` with `payload`: kept, then sent once every one before it has gone. */
    fun tell(context: Context, name: String, payload: JSONObject) {
        Outbox(dir(context)).add(name, payload)
        flush(context)
    }

    /** Send what is kept, oldest first, for as long as the worker takes them. */
    @JvmStatic fun flush(context: Context) {
        val app = context.applicationContext
        sender.execute {
            val skipped = Outbox(dir(app)).drain(::emit)
            if (skipped > 0) Log.w(TAG, "$skipped kept lines could not be read (a write cut short) and were passed over")
        }
    }

    private fun emit(name: String, payload: JSONObject): Boolean = try {
        Python.isStarted() && Python.getInstance().getModule("rook_android")
            .callAttr("emit", name, payload.toString()).toBoolean()
    } catch (t: Throwable) {
        Log.w(TAG, "$name could not go to the worker yet", t)
        false
    }
}

/**
 * The unsent events, one JSON line each ({name, payload}) in `unsent.jsonl`, and how many of its bytes have
 * been sent, in `unsent.sent`. An event is added at the end; [drain] sends from where the last one stopped.
 * When every line has gone the file starts again empty. No Android in here, so it is tested on the JVM.
 */
class Outbox(private val dir: File) {
    private val file = File(dir, "unsent.jsonl")
    private val mark = File(dir, "unsent.sent")

    fun add(name: String, payload: JSONObject) = synchronized(LOCK) {
        val line = JSONObject().put("name", name).put("payload", payload).toString()
        FileOutputStream(file, true).use { out ->
            if (!endsLine()) out.write('\n'.code)          // a line cut short before stays a line of its own
            out.write((line + "\n").toByteArray(Charsets.UTF_8))
            out.fd.sync()
        }
    }

    /**
     * Hand each unsent event to `send`, oldest first, until it says it could not take one: that one and the
     * rest stay for the next drain. Gives how many lines could not be read (a write cut short by a crash).
     */
    fun drain(send: (String, JSONObject) -> Boolean): Int = synchronized(DRAINING) {
        var skipped = 0
        while (true) {
            val (at, line) = synchronized(LOCK) { sent().let { it to lineAt(it) } }
            if (line == null) break
            val event = try { JSONObject(String(line, Charsets.UTF_8)) } catch (e: Exception) { null }
            if (event == null) skipped++
            else if (!send(event.getString("name"), event.getJSONObject("payload"))) return skipped
            synchronized(LOCK) { sent(at + line.size + 1) }
        }
        synchronized(LOCK) {
            if (file.exists() && sent() >= file.length()) { file.delete(); mark.delete() }
        }
        skipped
    }

    /** The events not sent yet, oldest first, as {name, payload}. */
    fun unsent(): JSONArray = synchronized(LOCK) {
        val out = JSONArray()
        var at = sent()
        while (true) {
            val line = lineAt(at) ?: break
            try { out.put(JSONObject(String(line, Charsets.UTF_8))) } catch (_: Exception) {}
            at += line.size + 1
        }
        out
    }

    private fun sent(): Long = try { mark.readText().trim().toLong() } catch (_: Exception) { 0L }

    private fun sent(bytes: Long) {
        val next = File(mark.path + ".new")
        next.writeText(bytes.toString())
        next.renameTo(mark)
    }

    /** The whole line that starts `at` bytes in, without its newline; null if none is complete there yet. */
    private fun lineAt(at: Long): ByteArray? {
        if (!file.exists() || at >= file.length()) return null
        FileInputStream(file).use { stream ->
            stream.channel.position(at)
            val input = stream.buffered()
            val line = ByteArrayOutputStream()
            while (true) {
                val b = input.read()
                if (b == -1) return null
                if (b == '\n'.code) return line.toByteArray()
                line.write(b)
            }
        }
    }

    private fun endsLine(): Boolean {
        if (!file.exists() || file.length() == 0L) return true
        RandomAccessFile(file, "r").use { it.seek(it.length() - 1); return it.read() == '\n'.code }
    }

    companion object {
        private val LOCK = Any()
        private val DRAINING = Any()
    }
}

/** A text in the inbox, as the SMS provider has it. */
data class Sms(val id: Long, val from: String?, val body: String?, val date: Long)

/**
 * Where the inbox was last told up to: the newest date told, and the ids told at that very millisecond (two
 * texts can share one, and the provider may give a deleted text's id to the next one, at a later date).
 */
data class SmsTold(val date: Long, val ids: Set<Long>) {
    fun json(): String = JSONObject().put("date", date).put("ids", JSONArray(ids.sorted())).toString()

    companion object {
        fun of(text: String): SmsTold {
            val o = JSONObject(text)
            val ids = o.getJSONArray("ids")
            return SmsTold(o.getLong("date"), (0 until ids.length()).map { ids.getLong(it) }.toSet())
        }

        /** The first look: the newest text there is now; history is not news (0.5's rule). */
        fun start(rows: List<Sms>): SmsTold {
            val newest = rows.maxOfOrNull { it.date } ?: return SmsTold(0, emptySet())
            return SmsTold(newest, rows.filter { it.date == newest }.map { it.id }.toSet())
        }
    }

    /** The texts not told yet among `rows` (any order), oldest first, and where that leaves the inbox told up to. */
    fun fresh(rows: List<Sms>): Pair<List<Sms>, SmsTold> {
        val news = rows.filter { it.date > date || (it.date == date && it.id !in ids) }
            .sortedWith(compareBy({ it.date }, { it.id }))
        var told = this
        for (m in news) told = if (m.date > told.date) SmsTold(m.date, setOf(m.id)) else SmsTold(told.date, told.ids + m.id)
        return news to told
    }
}

/** sms.received's payload: 0.5's {from, body, ts}, the body whole. */
fun smsReceived(m: Sms): JSONObject =
    JSONObject().put("from", m.from ?: "").put("body", m.body ?: "").put("ts", m.date / 1000.0)

/**
 * The watch on the inbox (brief AP7): `sms.received` for each text that comes in, once.
 *
 * It is a ContentObserver on the SMS provider in the worker's process, not the SMS_RECEIVED broadcast:
 * - the broadcast needs RECEIVE_SMS, another permission, where this needs only READ_SMS, which sms.list asks for;
 * - the broadcast gives the message in PDUs, with no id to tell it by, and a long text in several parts; the
 *   provider has each text as one row, whole, with an id and the time it came;
 * - what came while the process was not running (before the service starts after a reboot, or after Android
 *   stopped it) is read on [start] from where the inbox was last told, so nothing is missed and nothing is told
 *   again. The process lives as long as the worker's foreground service, with the app swiped away.
 *
 * It starts once READ_SMS is granted ([WorkerService] calls [start] when it starts, when a permission is given
 * and at each link) and never asks for it.
 */
object SmsWatch {
    private const val TAG = "RookSms"
    private val INBOX: Uri = Uri.parse("content://sms/inbox")
    private val lock = Any()
    private var observer: ContentObserver? = null

    @JvmStatic fun start(context: Context) {
        val app = context.applicationContext
        if (app.checkSelfPermission(Manifest.permission.READ_SMS) != PackageManager.PERMISSION_GRANTED) return
        val handler = synchronized(lock) {
            if (observer != null) null else {
                val thread = HandlerThread("rook-sms").apply { start() }
                Handler(thread.looper).also { h ->
                    observer = object : ContentObserver(h) {
                        override fun onChange(selfChange: Boolean) = look(app)
                    }.also { app.contentResolver.registerContentObserver(Uri.parse("content://sms"), true, it) }
                }
            }
        }
        handler?.post { look(app) }         // what came while it was not watching
    }

    private fun look(app: Context) {
        try {
            synchronized(lock) {
                val kept = File(Events.dir(app), "sms-told.json")
                val told = if (kept.exists()) SmsTold.of(kept.readText()) else null
                val rows = inbox(app, told?.date)
                val (news, next) = told?.fresh(rows) ?: (emptyList<Sms>() to SmsTold.start(rows))
                for (m in news) Events.tell(app, "sms.received", smsReceived(m))
                if (next != told) File(kept.path + ".new").apply { writeText(next.json()) }.renameTo(kept)
            }
        } catch (e: SecurityException) {
            Log.w(TAG, "READ_SMS was taken back: the inbox is not watched until it is given again", e)
        } catch (e: Exception) {
            Log.w(TAG, "the inbox could not be read", e)
        }
    }

    /** The inbox from `since` (ms, that millisecond included) on; every text when null. */
    private fun inbox(app: Context, since: Long?): List<Sms> {
        val cursor = app.contentResolver.query(INBOX, arrayOf("_id", "address", "body", "date"),
            since?.let { "date >= ?" }, since?.let { arrayOf(it.toString()) }, "date ASC") ?: return emptyList()
        val rows = ArrayList<Sms>()
        cursor.use {
            while (it.moveToNext()) rows.add(Sms(it.getLong(0), it.getString(1), it.getString(2), it.getLong(3)))
        }
        return rows
    }
}

/** Another app's notification as it was posted: what notification-android.received is made of. */
data class Posted(val key: String, val pkg: String, val title: String?, val text: String?, val bigText: String?, val postTime: Long)

/** notification-android.received's payload: 0.5's {key, package, title, text, ts}; text is the long text when there is one, whole. */
fun notificationReceived(p: Posted): JSONObject =
    JSONObject().put("key", p.key).put("package", p.pkg).put("title", p.title ?: "")
        .put("text", p.bigText?.takeIf { it.isNotEmpty() } ?: p.text ?: "").put("ts", p.postTime / 1000.0)

/**
 * Where the notifications were last told up to: the newest post time told, and the keys told at that very
 * millisecond. Used only when the listener connects, to tell what was posted while it was not bound; while
 * it is bound, Android hands it each post once and each is told.
 */
data class NoticesTold(val ms: Long, val keys: Set<String>) {
    fun json(): String = JSONObject().put("ms", ms).put("keys", JSONArray(keys.sorted())).toString()

    fun after(p: Posted): NoticesTold = when {
        p.postTime > ms -> NoticesTold(p.postTime, setOf(p.key))
        p.postTime == ms -> NoticesTold(ms, keys + p.key)
        else -> this
    }

    /** Of what is in the shade, the posts not told yet that are not Rook's own (`own`), oldest first. */
    fun missed(shade: List<Posted>, own: String): List<Posted> =
        shade.filter { it.pkg != own && (it.postTime > ms || (it.postTime == ms && it.key !in keys)) }.sortedBy { it.postTime }

    companion object {
        fun of(text: String): NoticesTold {
            val o = JSONObject(text)
            val keys = o.getJSONArray("keys")
            return NoticesTold(o.getLong("ms"), (0 until keys.length()).map { keys.getString(it) }.toSet())
        }
    }
}

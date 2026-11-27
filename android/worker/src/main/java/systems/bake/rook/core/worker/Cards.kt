package systems.bake.rook.core.worker

import android.app.Activity
import android.app.AlertDialog
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.os.Build
import android.os.Bundle
import android.util.Log
import com.chaquo.python.Python
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import kotlin.concurrent.thread

/**
 * The phone's own notifications (brief AP4): what an agent wants to tell the person, posted by
 * notification.post on channels of their own, apart from the worker's ongoing notification and the
 * asks [Bridge] posts.
 *
 * A plain one is a title and a text. A card (notification.post with an id) is something the person
 * answers, with 0.5's buttons on the notification itself: Yes, No, a choice between options, More
 * details, Got it. What the person does with a card travels back to the hub as the event
 * `notification-android.card` ({id, type: decision | acked | dismissed | opened, decision: approve |
 * deny | choose, choice: the option, verbatim, ts}):
 *
 *     the button -> [CardReceiver] (or [CardOpened], a tap on the card) -> the answer is written to
 *     files/notification-android/answers.json -> [flush], on a thread of its own, hands each to the
 *     running worker (rook_android.emit -> the worker's own emit) -> the hub's event bus (J2), where it
 *     is journaled and jobs and agents match it.
 *
 * An answer given while the worker is not linked waits in that file and goes when it links again
 * ([WorkerService] calls [flush] at each link). notification.update shows what came of a card.
 */
object Cards {
    private const val TAG = "RookCards"
    private val lock = Any()
    private val ACTIONS = listOf("yes", "no", "choose", "explain", "ack")
    private val LABELS = mapOf("yes" to "Yes", "no" to "No", "choose" to "Choose", "explain" to "More details", "ack" to "Got it")
    private val CHANNELS = mapOf("urgent" to "rook_urgent", "family" to "rook_family", "approvals" to "rook_approvals",
        "digest" to "rook_digest", "jobs" to "rook_jobs")
    private const val MESSAGES = "rook_msgs"
    private const val WAITING = "rook_waiting"
    internal const val ID = "id"
    internal const val DO = "do"
    internal const val CHOICE = "choice"
    internal const val OPTION = "option"

    // ---- notification.post / notification.update (called from Python) -------------------------

    /** A plain notification: {id, channel}. */
    @JvmStatic fun post(title: String, text: String, channel: String): String {
        val app = Bridge.context()
        channels(app)
        val id = CHANNELS[channel] ?: MESSAGES
        val nid = (System.currentTimeMillis() and 0x7fffffff).toInt()
        val b = Notification.Builder(app, id).setContentTitle(title).setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setSmallIcon(android.R.drawable.stat_notify_chat).setAutoCancel(true)
        app.packageManager.getLaunchIntentForPackage(app.packageName)?.let {
            b.setContentIntent(PendingIntent.getActivity(app, nid, it, PendingIntent.FLAG_IMMUTABLE))
        }
        app.getSystemService(NotificationManager::class.java).notify(nid, b.build())
        return JSONObject().put("id", nid).put("channel", id).toString()
    }

    /** A card from notification.post's checked arguments (JSON): {id, new}. Posting a known id again updates it without a sound. */
    @JvmStatic fun card(json: String): String {
        val app = Bridge.context()
        val given = JSONObject(json)
        val id = given.getString("id")
        val new = synchronized(lock) {
            val cards = read(app, "cards.json", JSONObject())
            val new = !cards.has(id)
            cards.put(id, given.put("state", "pending"))
            write(app, "cards.json", cards)
            new
        }
        show(app, given, ring = new)
        return JSONObject().put("id", id).put("new", new).toString()
    }

    /** notification.update: done (a confirmation without buttons), failed (shows message) or gone (taken away). {id} or {error}. */
    @JvmStatic fun update(id: String, state: String, message: String): String {
        val app = Bridge.context()
        val card = synchronized(lock) {
            val cards = read(app, "cards.json", JSONObject())
            val card = cards.optJSONObject(id)
                ?: return JSONObject().put("error", "no card has the id $id on this phone; notification.post with id makes one").toString()
            card.put("state", state).put("message", message)
            write(app, "cards.json", cards)
            card
        }
        if (state == "gone") cancel(app, id) else show(app, card, ring = false)
        return JSONObject().put("id", id).toString()
    }

    // ---- what the person did ------------------------------------------------------------------

    /** A button, a swipe or a tap on card `id`: keep the answer, show what it did, send it on. */
    internal fun answered(context: Context, id: String, act: String, choice: String?) {
        val app = context.applicationContext
        Bridge.attach(app)
        val event = JSONObject().put("id", id).put("ts", System.currentTimeMillis() / 1000.0)
        val card = synchronized(lock) {
            val cards = read(app, "cards.json", JSONObject())
            val card = cards.optJSONObject(id) ?: return
            when (act) {
                "yes" -> { event.put("type", "decision").put("decision", "approve"); card.put("state", "sent").put("sent", "Yes") }
                "no" -> { event.put("type", "decision").put("decision", "deny"); card.put("state", "sent").put("sent", "No") }
                "choose" -> {
                    val options = card.optJSONArray("options") ?: JSONArray()
                    val picked = (0 until options.length()).map { options.getString(it) }.firstOrNull { it == choice } ?: return
                    event.put("type", "decision").put("decision", "choose").put("choice", picked)
                    card.put("state", "sent").put("sent", picked)
                }
                "ack" -> { event.put("type", "acked"); card.put("state", "acked") }
                "explain" -> card.put("explained", true)
                "dismissed", "opened" -> {
                    if (card.optBoolean(act)) return          // once per card, as 0.5
                    event.put("type", act); card.put(act, true)
                }
                else -> return
            }
            write(app, "cards.json", cards)
            if (event.has("type")) {
                write(app, "answers.json", read(app, "answers.json", JSONArray()).put(event))
            }
            card
        }
        when (card.optString("state")) {
            "acked" -> cancel(app, id)
            else -> if (act != "dismissed" && act != "opened") show(app, card, ring = false)
        }
        flush(app)
    }

    /** Hand every kept answer to the running worker, oldest first; what it cannot take yet stays for the next link. */
    @JvmStatic fun flush(context: Context) {
        val app = context.applicationContext
        thread(name = "rook-card-answers", isDaemon = true) {
            synchronized(flushing) {
                while (true) {
                    val next = synchronized(lock) { read(app, "answers.json", JSONArray()).optJSONObject(0) } ?: break
                    val sent = try {
                        Python.isStarted() && Python.getInstance().getModule("rook_android")
                            .callAttr("emit", "notification-android.card", next.toString()).toBoolean()
                    } catch (t: Throwable) {
                        Log.w(TAG, "an answer could not go to the worker yet", t)
                        false
                    }
                    if (!sent) break
                    synchronized(lock) {
                        val kept = read(app, "answers.json", JSONArray())
                        kept.remove(0)
                        write(app, "answers.json", kept)
                    }
                }
            }
        }
    }

    private val flushing = Any()

    // ---- the notification -------------------------------------------------------------------

    private fun nid(id: String) = "card:$id".hashCode()

    private fun cancel(app: Context, id: String) = app.getSystemService(NotificationManager::class.java).cancel(nid(id))

    private fun show(app: Context, card: JSONObject, ring: Boolean) {
        channels(app)
        val id = card.getString("id")
        val line = card.optString("text")
        val body = card.optString("body")
        val explain = card.optString("explain")
        val state = card.optString("state", "pending")
        val actions = strings(card.optJSONArray("actions"))
        val urgent = card.optString("urgency") == "urgent" || card.optString("channel") == "urgent"
        val answerable = actions.any { it == "yes" || it == "no" || it == "choose" }
        val channel = when {
            urgent -> CHANNELS.getValue("urgent")
            card.optString("channel") == "digest" && answerable -> WAITING
            else -> CHANNELS[card.optString("channel")] ?: CHANNELS.getValue("approvals")
        }
        val shown = when (state) {
            "sent" -> "Sent: ${card.optString("sent")}. $line"
            "done" -> "Done" + card.optString("message").let { if (it.isBlank()) ". $line" else ": $it" }
            "failed" -> "Didn't go through: ${card.optString("message").ifBlank { line }}"
            else -> line
        }
        val detail = buildString {          // under the line when the card is expanded: its body, and More details once asked
            if (state == "pending") {
                append(body)
                if (card.optBoolean("explained") && explain.isNotBlank()) {
                    if (isNotEmpty()) append("\n\n")
                    append(explain)
                }
            }
        }
        val b = Notification.Builder(app, channel)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle(card.optString("source").ifBlank { "Rook" })
            .setContentText(shown)
            .setStyle(if (detail.isBlank()) Notification.BigTextStyle().bigText(shown)
                else Notification.BigTextStyle().setBigContentTitle(line).bigText(detail))
            .setOnlyAlertOnce(!ring)
            .setCategory(if (urgent) Notification.CATEGORY_ALARM else Notification.CATEGORY_REMINDER)
            .setVisibility(if (urgent) Notification.VISIBILITY_PUBLIC else Notification.VISIBILITY_PRIVATE)
            .setContentIntent(PendingIntent.getActivity(app, nid(id),
                Intent(app, CardOpened::class.java).putExtra(ID, id).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT))
            .setDeleteIntent(broadcast(app, id, "dismissed"))
        if (state == "pending") {
            // Android shows a notification's first three buttons. A question's options are buttons of their own when
            // they fit beside the card's other buttons; otherwise one Choose button opens the list of every option.
            val buttons = ACTIONS.filter { it in actions && !(it == "explain" && (explain.isBlank() || card.optBoolean("explained"))) }
            val options = strings(card.optJSONArray("options"))
            val inline = "choose" in buttons && buttons.size - 1 + options.size <= 3
            for (act in buttons) {
                when {
                    act == "choose" && inline -> options.forEachIndexed { i, option ->
                        b.addAction(Notification.Action.Builder(null, option, broadcast(app, id, act, i)).build())
                    }
                    act == "choose" -> b.addAction(Notification.Action.Builder(null, LABELS[act],
                        PendingIntent.getActivity(app, "$id/choose".hashCode(),
                            Intent(app, CardOpened::class.java).putExtra(ID, id).putExtra(CHOICE, true)
                                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)).build())
                    else -> b.addAction(Notification.Action.Builder(null, LABELS[act], broadcast(app, id, act)).build())
                }
            }
        }
        app.getSystemService(NotificationManager::class.java).notify(nid(id), b.build())
    }

    /** A button's broadcast; `option`, for an option's own button, is its place in the card's options. */
    private fun broadcast(app: Context, id: String, act: String, option: Int = -1): PendingIntent =
        PendingIntent.getBroadcast(app, "$id/$act/$option".hashCode(),
            Intent(app, CardReceiver::class.java).setAction("$id/$act/$option").putExtra(ID, id).putExtra(DO, act)
                .putExtra(OPTION, option),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)

    /** A card's options, for the list a Choose button opens: null when it is not waiting on a choice. */
    internal fun options(context: Context, id: String): List<String>? = synchronized(lock) {
        val card = read(context.applicationContext, "cards.json", JSONObject()).optJSONObject(id) ?: return null
        if (card.optString("state") != "pending") return null
        strings(card.optJSONArray("options")).ifEmpty { null }
    }

    /** The channels, as 0.5 had them: Android keeps the person's choices for each. */
    private fun channels(app: Context) {
        val alarm = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
            ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)
        val alarmAudio = AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM)
            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build()
        app.getSystemService(NotificationManager::class.java).createNotificationChannels(listOf(
            NotificationChannel(MESSAGES, "Rook messages", NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "What Rook's agents tell you"
            },
            NotificationChannel(CHANNELS.getValue("urgent"), "Urgent", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Emergencies and urgent decisions: rings like an alarm, even in Do Not Disturb when Rook may"
                setSound(alarm, alarmAudio); enableVibration(true); vibrationPattern = longArrayOf(0, 600, 300, 600, 300, 600)
                setBypassDnd(true); lockscreenVisibility = Notification.VISIBILITY_PUBLIC
            },
            NotificationChannel(CHANNELS.getValue("family"), "Family", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Family, school and health"; enableVibration(true)
            },
            NotificationChannel(CHANNELS.getValue("approvals"), "Decisions", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Yes/No decisions from Rook"; enableVibration(true)
            },
            NotificationChannel(WAITING, "Waiting decisions", NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "Decisions that can wait: in the shade without popping up"
            },
            NotificationChannel(CHANNELS.getValue("digest"), "Digest", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Summaries"
            },
            NotificationChannel(CHANNELS.getValue("jobs"), "Jobs", NotificationManager.IMPORTANCE_MIN).apply {
                description = "Background job updates"; setSound(null, null); enableVibration(false)
            },
        ))
    }

    private fun strings(a: JSONArray?) = (0 until (a?.length() ?: 0)).map { a!!.getString(it) }

    private fun file(app: Context, name: String) = File(File(app.filesDir, "notification-android").apply { mkdirs() }, name)

    private fun <T> read(app: Context, name: String, empty: T): T {
        val f = file(app, name)
        if (!f.exists()) return empty
        @Suppress("UNCHECKED_CAST")
        return try {
            (if (empty is JSONArray) JSONArray(f.readText()) else JSONObject(f.readText())) as T
        } catch (e: Exception) {
            Log.w(TAG, "$name could not be read; starting it again", e)
            empty
        }
    }

    private fun write(app: Context, name: String, value: Any) {
        val f = file(app, name)
        val next = File(f.path + ".new")
        next.writeText(value.toString())
        next.renameTo(f)
    }
}

/** A card's buttons and its swipe-away. */
class CardReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val id = intent.getStringExtra(Cards.ID) ?: return
        val act = intent.getStringExtra(Cards.DO) ?: return
        val option = intent.getIntExtra(Cards.OPTION, -1)
        Cards.answered(context, id, act, if (option >= 0) Cards.options(context, id)?.getOrNull(option) else null)
    }
}

/**
 * A tap on a card: the card was opened (an event, once), and the Rook app comes to the front. From a card's
 * Choose button: Android's own list of every one of its options, the one tapped sent back as the choice.
 */
class CardOpened : Activity() {
    override fun onCreate(saved: Bundle?) {
        super.onCreate(saved)
        val id = intent.getStringExtra(Cards.ID) ?: return finish()
        val options = if (intent.getBooleanExtra(Cards.CHOICE, false)) Cards.options(this, id) else null
        if (options == null) {
            Cards.answered(this, id, "opened", null)
            packageManager.getLaunchIntentForPackage(packageName)?.let { startActivity(it) }
            finish()
            return
        }
        AlertDialog.Builder(this)
            .setItems(options.toTypedArray()) { _, picked -> Cards.answered(this, id, "choose", options[picked]) }
            .setOnDismissListener { finish() }
            .show()
    }
}

package systems.bake.rook.core.worker

import android.app.Activity
import android.app.AlertDialog
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.RemoteInput
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.os.Build
import android.os.Bundle
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * The phone's own notifications (brief AP4): what an agent wants to tell the person, posted by
 * notification.post on channels of their own, apart from the worker's ongoing notification and the
 * asks [Bridge] posts.
 *
 * A plain one is a title and a text. A card (notification.post with an id) is something the person
 * answers, with 0.5's buttons on the notification itself: Yes, No, a choice between options, More
 * details, Got it. What the person does with a card travels back to the hub as the event
 * `notification-android.card` ({id, type: decision | acked | dismissed | opened, decision: approve |
 * deny | choose, choice: the option, verbatim, reason: why No, as typed, ts}):
 *
 *     the button -> [CardReceiver] (or [CardOpened], a tap on the card) -> [Events.tell], which keeps it
 *     with the phone's other unsent events and hands them, oldest first, to the running worker
 *     (rook_android.emit -> the worker's own emit) -> the hub's event bus (J2), where it is journaled and
 *     jobs and agents match it.
 *
 * An answer given while the worker is not linked waits there and goes when it links again.
 * notification.update shows what came of a card.
 *
 * No has a reply field (brief AP7): what the person types there goes as `reason`, 0.5's field. Android
 * sends a reply field only with something typed in it, so the field also offers one ready answer, a chip,
 * that sends a plain No ([PLAIN_NO]).
 *
 * A card's `expires_at` (epoch seconds; 0 or none: never) is the caller's own request: at that time Android
 * takes the card from the shade (setTimeoutAfter) and the card's state is `expired`. The hub is told nothing
 * of it, as 0.5 told nothing; notification.update on it afterwards answers and shows nothing, as 0.5's did.
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
    internal const val REASON = "reason"
    internal const val PLAIN_NO = "Just No"

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
            val new = cards.optJSONObject(id)?.let { lapsed(it) } ?: true      // an expired card's id makes a new one
            cards.put(id, given.put("state", if (due(given)) "expired" else "pending"))
            write(app, "cards.json", cards)
            new
        }
        if (lapsed(given)) cancel(app, id) else show(app, given, ring = new)
        return JSONObject().put("id", id).put("new", new).toString()
    }

    /** notification.update: done (a confirmation without buttons), failed (shows message) or gone (taken away). {id} or {error}. */
    @JvmStatic fun update(id: String, state: String, message: String): String {
        val app = Bridge.context()
        val card = synchronized(lock) {
            val cards = read(app, "cards.json", JSONObject())
            val card = cards.optJSONObject(id)
                ?: return JSONObject().put("error", "no card has the id $id on this phone; notification.post with id makes one").toString()
            if (!lapse(card)) card.put("state", state).put("message", message)
            write(app, "cards.json", cards)
            card
        }
        if (state == "gone" || lapsed(card)) cancel(app, id) else show(app, card, ring = false)
        return JSONObject().put("id", id).toString()
    }

    // ---- what the person did ------------------------------------------------------------------

    /**
     * A button, a swipe or a tap on card `id`: keep the answer, show what it did, send it on. `reason` is what
     * was typed into No's reply field. On a card whose expires_at has come (Android's own removal at that time
     * comes here as a swipe) nothing is sent: it is expired.
     */
    internal fun answered(context: Context, id: String, act: String, choice: String?, reason: String? = null) {
        val app = context.applicationContext
        Bridge.attach(app)
        val event = JSONObject().put("id", id).put("ts", System.currentTimeMillis() / 1000.0)
        val card = synchronized(lock) {
            val cards = read(app, "cards.json", JSONObject())
            val card = cards.optJSONObject(id) ?: return
            if (lapse(card)) {
                write(app, "cards.json", cards)
                cancel(app, id)
                return
            }
            when (act) {
                "yes" -> { event.put("type", "decision").put("decision", "approve"); card.put("state", "sent").put("sent", "Yes") }
                "no" -> {
                    event.put("type", "decision").put("decision", "deny")
                    reason?.trim()?.takeIf { it.isNotEmpty() }?.let { event.put("reason", it) }
                    card.put("state", "sent").put("sent", "No")
                }
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
            if (event.has("type")) Events.tell(app, "notification-android.card", event)
            card
        }
        when (card.optString("state")) {
            "acked" -> cancel(app, id)
            else -> if (act != "dismissed" && act != "opened") show(app, card, ring = false)
        }
    }

    // ---- a card's own expires_at (brief AP7) --------------------------------------------------

    /** Whether `card`'s expires_at has come. */
    private fun due(card: JSONObject) = card.optDouble("expires_at", 0.0).let { it > 0 && System.currentTimeMillis() >= it * 1000 }

    /** Whether `card` is expired, or should now be. */
    private fun lapsed(card: JSONObject) = card.optString("state") == "expired" || due(card)

    /** Mark `card` expired if its time has come: whether it is expired. */
    private fun lapse(card: JSONObject): Boolean {
        if (!lapsed(card)) return false
        card.put("state", "expired")
        return true
    }

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
        val expires = card.optDouble("expires_at", 0.0)
        if (expires > 0 && Build.VERSION.SDK_INT >= 26) {
            b.setTimeoutAfter(maxOf(1L, (expires * 1000).toLong() - System.currentTimeMillis()))
        }
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
                    act == "no" -> b.addAction(Notification.Action.Builder(null, LABELS[act], broadcast(app, id, act, reply = true))
                        .addRemoteInput(RemoteInput.Builder(REASON).setLabel("Why not? (or $PLAIN_NO)")
                            .setChoices(arrayOf(PLAIN_NO)).build()).build())
                    else -> b.addAction(Notification.Action.Builder(null, LABELS[act], broadcast(app, id, act)).build())
                }
            }
        }
        app.getSystemService(NotificationManager::class.java).notify(nid(id), b.build())
    }

    /**
     * A button's broadcast; `option`, for an option's own button, is its place in the card's options. A button
     * with a reply field (`reply`) is mutable, so Android can put what was typed into it.
     */
    private fun broadcast(app: Context, id: String, act: String, option: Int = -1, reply: Boolean = false): PendingIntent =
        PendingIntent.getBroadcast(app, "$id/$act/$option".hashCode(),
            Intent(app, CardReceiver::class.java).setAction("$id/$act/$option").putExtra(ID, id).putExtra(DO, act)
                .putExtra(OPTION, option),
            PendingIntent.FLAG_UPDATE_CURRENT or when {
                !reply -> PendingIntent.FLAG_IMMUTABLE
                Build.VERSION.SDK_INT >= 31 -> PendingIntent.FLAG_MUTABLE
                else -> 0
            })

    /** A card's options, for the list a Choose button opens: null when it is not waiting on a choice. */
    internal fun options(context: Context, id: String): List<String>? = synchronized(lock) {
        val card = read(context.applicationContext, "cards.json", JSONObject()).optJSONObject(id) ?: return null
        if (card.optString("state") != "pending" || due(card)) return null
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

    private fun file(app: Context, name: String) = File(File(Storage.files(app), "notification-android").apply { mkdirs() }, name)

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
        Cards.answered(context, id, act, if (option >= 0) Cards.options(context, id)?.getOrNull(option) else null, reason(intent))
    }

    /** What was typed into No's reply field; null for nothing, and for its ready answer, a plain No. */
    private fun reason(intent: Intent): String? {
        val typed = RemoteInput.getResultsFromIntent(intent)?.getCharSequence(Cards.REASON)?.toString() ?: return null
        val chip = if (Build.VERSION.SDK_INT >= 28) RemoteInput.getResultsSource(intent) == RemoteInput.SOURCE_CHOICE
            else typed == Cards.PLAIN_NO
        return if (chip) null else typed
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

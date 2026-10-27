package systems.bake.rook

import android.Manifest
import android.app.KeyguardManager
import android.app.Notification
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.ContactsContract
import android.service.notification.StatusBarNotification
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.concurrent.CopyOnWriteArraySet
import java.util.concurrent.Executors

/**
 * Attention on the phone (docs/design/attention-phone.md): decision cards the hub posts,
 * the event log it polls (decisions, seen/opened/dismissed/acked, unlocked, notices), and the
 * quiet feed of classified notifications. Persisted under files/attention/, so it all works
 * offline and waits for the next attention.poll. Called from Python (attention_android.py),
 * the notification listener, the card screen and WorkerService.
 */
object AttentionStore {
    private const val TAG = "RookAttention"
    private val lock = Any()
    private var dir: File? = null
    /** The app, once the store has loaded: what [changed] redraws the home-screen widgets with. */
    private var app: Context? = null
    private val book = CardBook()
    private val log = EventLog()
    private val quiet = QuietFeed()
    private var defaults = JSONObject()
    private var override: JSONObject? = null
    private var rules = AttentionRules(emptyList())
    /** Cards that just finished (done/gone/acked), so the card screen can show the outcome. */
    private val outcomes = object : LinkedHashMap<String, AttentionCard>() {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, AttentionCard>?) = size > 50
    }
    /** Last content seen per notification key: updates that change nothing are not classified twice. */
    private val lastContent = object : LinkedHashMap<String, Int>() {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, Int>?) = size > 300
    }
    /** The original app's tap target for a local alert (memory only). */
    private val targets = object : LinkedHashMap<String, PendingIntent>() {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, PendingIntent>?) = size > 50
    }
    private var lastId = 0L
    private var starred: List<String> = emptyList()
    private var starredAt = 0L
    private val listeners = CopyOnWriteArraySet<Runnable>()
    private val main = Handler(Looper.getMainLooper())
    private val worker = Executors.newSingleThreadExecutor()

    private fun now() = System.currentTimeMillis() / 1000.0

    // ---- persistence (callers hold [lock]) ----------------------------------

    private fun ensure(ctx: Context) {
        if (dir != null) return
        val d = File(ctx.applicationContext.filesDir, "attention").apply { mkdirs() }
        fun read(name: String) = try { File(d, name).takeIf { it.exists() }?.readText() } catch (_: Exception) { null }
        runCatching { book.load(read("cards.json")?.let { JSONArray(it) }) }
        runCatching { log.load(read("events.json")?.let { JSONObject(it) }) }
        runCatching { quiet.load(read("feed.json")?.let { JSONObject(it) }, now()) }
        override = runCatching { read("rules.json")?.let { JSONObject(it) } }.getOrNull()
        defaults = runCatching { ctx.assets.open("attention_rules.json").use { JSONObject(String(it.readBytes(), Charsets.UTF_8)) } }
            .getOrElse { JSONObject() }
        rules = AttentionRules.effective(defaults, override)
        app = ctx.applicationContext
        dir = d
    }

    private fun write(name: String, text: String) {
        val d = dir ?: return
        try {
            val tmp = File(d, "$name.tmp")
            tmp.writeText(text)
            if (!tmp.renameTo(File(d, name))) { File(d, name).writeText(text); tmp.delete() }
        } catch (e: Exception) { Log.w(TAG, "save $name failed", e) }
    }
    private fun saveCards() = write("cards.json", book.toJson().toString())
    private fun saveEvents() = write("events.json", log.toJson().toString())
    private fun saveFeed() = write("feed.json", quiet.toJson().toString())

    private fun event(type: String, fields: JSONObject = JSONObject()) { log.add(type, fields, now()); saveEvents() }

    private fun changed() { main.post { listeners.forEach { runCatching { it.run() } }; app?.let { RookWidgets.refresh(it) } } }
    fun addListener(l: Runnable) { listeners.add(l) }
    fun removeListener(l: Runnable) { listeners.remove(l) }

    private fun err(message: String) = JSONObject().put("ok", false).put("error", message).toString()

    // ---- attention.* (Python) ----------------------------------------------

    @JvmStatic fun post(ctx: Context, json: String): String {
        val app = ctx.applicationContext
        val (card, isNew) = try {
            synchronized(lock) {
                ensure(app)
                val c = AttentionCard.fromPost(JSONObject(json), now())
                val isNew = book.post(c)
                saveCards()
                book[c.id]!! to isNew
            }
        } catch (e: Exception) { return err(e.message ?: "bad card") }
        show(app, card, isNew)
        changed()
        return JSONObject().put("ok", true).put("id", card.id).put("new", isNew).toString()
    }

    @JvmStatic fun update(ctx: Context, id: String, state: String, message: String): String {
        if (state !in setOf("done", "failed", "gone")) return err("state must be done, failed or gone")
        val app = ctx.applicationContext
        val card = synchronized(lock) {
            ensure(app)
            val c = book.update(id, state, message)
            if (c == null) {
                // Already acked or dismissed here: the hub's follow-up still succeeds.
                return if (outcomes.containsKey(id)) JSONObject().put("ok", true).put("id", id).toString() else err("unknown card")
            }
            if (state != "failed") outcomes[id] = c
            saveCards(); c
        }
        if (state == "failed") AttentionChannels.postCard(app, card, ring = false) else AttentionChannels.cancel(app, id)
        changed()
        return JSONObject().put("ok", true).put("id", id).toString()
    }

    @JvmStatic fun poll(ctx: Context, since: Long): String = synchronized(lock) {
        ensure(ctx.applicationContext)
        expire(ctx.applicationContext)
        log.poll(since).toString()
    }

    @JvmStatic fun feed(ctx: Context, limit: Int): String = synchronized(lock) {
        ensure(ctx.applicationContext)
        quiet.prune(now())
        val out = JSONArray()
        // Only the summary fields: the same privacy rule as notice events.
        quiet.recent(limit.coerceIn(1, 300)).forEach { e ->
            out.put(JSONObject().put("id", e.optString("id")).put("ts", e.optDouble("ts")).put("app", e.optString("app"))
                .put("category", e.optString("category")).put("priority", e.optString("priority")).put("summary", e.optString("summary")))
        }
        JSONObject().put("ok", true).put("entries", out).put("ignored_7d", quiet.ignoredCount()).toString()
    }

    @JvmStatic fun rulesGet(ctx: Context): String = synchronized(lock) {
        ensure(ctx.applicationContext)
        val o = override
        JSONObject().put("ok", true).put("source", if (o == null) "default" else "override")
            .put("extend_default", o?.optBoolean("extend_default", false) ?: false)
            .put("rules", o ?: defaults).put("effective_rules", rules.rules.size).toString()
    }

    /** Replace the rules with [json] (a {rules, family_names, ...} document, or a bare list that extends the defaults); empty resets. */
    @JvmStatic fun rulesSet(ctx: Context, json: String?): String { synchronized(lock) {
        ensure(ctx.applicationContext)
        val text = json?.trim().orEmpty()
        val doc: JSONObject? = try {
            when {
                text.isEmpty() || text == "null" -> null
                text.startsWith("[") -> JSONObject().put("rules", JSONArray(text)).put("extend_default", true)
                else -> JSONObject(text)
            }
        } catch (e: Exception) { return err("rules must be JSON: ${e.message}") }
        if (doc != null) {
            val given = doc.optJSONArray("rules")?.length() ?: 0
            val valid = AttentionRules.rulesOf(doc.optJSONArray("rules")).size
            if (given != valid) return err("${given - valid} of $given rules are invalid (need priority urgent|family|normal|ignore and a non-empty match)")
        }
        override = doc
        rules = AttentionRules.effective(defaults, doc)
        if (doc == null) File(dir, "rules.json").delete() else write("rules.json", doc.toString())
        return JSONObject().put("ok", true).put("source", if (doc == null) "default" else "override").put("effective_rules", rules.rules.size).toString()
    } }

    /**
     * attention.remind finished (SpokenReminder): one `reminded` event for the hub's poll. Only the
     * outcome, the speech route and two rounded level numbers; never audio or anything heard.
     */
    fun reminded(ctx: Context, id: String, outcome: String, via: String?, level: QuietCheck.Result?, checked: Boolean?) {
        try {
            synchronized(lock) {
                ensure(ctx.applicationContext)
                val f = JSONObject().put("id", id).put("outcome", outcome)
                if (via != null) f.put("via", via)
                if (level != null) f.put("level", JSONObject().put("active_frac", level.activeFrac.coerceIn(0.0, 1.0))
                    .put("peak_dbfs", level.peakDbfs))
                if (checked != null) f.put("checked", checked)
                event("reminded", f)
            }
        } catch (e: Exception) { Log.w(TAG, "reminded event failed", e) }
    }

    // ---- the card screen and the app ----------------------------------------

    fun showing(ctx: Context): List<AttentionCard> = synchronized(lock) { ensure(ctx.applicationContext); book.showing(now()) }
    /** The card screen's stack: sent decisions are out of it until the hub refuses them (see [CardBook.stack]). */
    fun stack(ctx: Context): List<AttentionCard> = synchronized(lock) { ensure(ctx.applicationContext); book.stack(now()) }
    fun card(ctx: Context, id: String): AttentionCard? = synchronized(lock) { ensure(ctx.applicationContext); book[id] }
    fun outcome(id: String): AttentionCard? = synchronized(lock) { outcomes[id] }
    fun target(id: String): PendingIntent? = synchronized(lock) { targets[id] }
    fun recentFeed(ctx: Context, limit: Int): Pair<List<JSONObject>, Int> = synchronized(lock) {
        ensure(ctx.applicationContext); quiet.prune(now()); quiet.recent(limit) to quiet.ignoredCount()
    }

    /**
     * Yes / No on a card: a "decision" event, saved at once; the hub reads it on its next poll (an
     * unread event is read on the one after, so nothing is lost offline) and answers with attention.update.
     * The card screen does not wait for that: the card leaves its stack now.
     */
    fun decide(ctx: Context, id: String, approve: Boolean, reason: String = "") {
        val app = ctx.applicationContext
        val card = synchronized(lock) {
            ensure(app)
            val c = book[id] ?: return
            c.state = "sent"; c.decision = if (approve) "approve" else "deny"; c.message = ""; c.sentAt = now()
            val f = JSONObject().put("id", id).put("decision", c.decision)
            if (!approve && reason.isNotBlank()) f.put("reason", reason.trim().take(200))
            event("decision", f); saveCards(); c
        }
        AttentionChannels.postCard(app, card, ring = false)
        changed()
    }

    /**
     * One of a question's options tapped: a "decision" event with decision "choose" and the option's
     * text exactly as the hub sent it (never a bare Yes, which would not say which one was meant).
     * Anything that is not one of the card's options is ignored.
     */
    fun choose(ctx: Context, id: String, option: String) {
        val app = ctx.applicationContext
        val card = synchronized(lock) {
            ensure(app)
            val c = book[id] ?: return
            val picked = c.chosen(option) ?: return
            c.state = "sent"; c.decision = "choose"; c.choice = picked; c.message = ""; c.sentAt = now()
            event("decision", JSONObject().put("id", id).put("decision", "choose").put("choice", picked)); saveCards(); c
        }
        AttentionChannels.postCard(app, card, ring = false)
        changed()
    }

    /** "Got it" on an alert: an "acked" event and the card goes away. */
    fun ack(ctx: Context, id: String) {
        val app = ctx.applicationContext
        synchronized(lock) {
            ensure(app)
            val c = book.remove(id) ?: return
            c.state = "done"; outcomes[id] = c
            event("acked", JSONObject().put("id", id)); saveCards()
        }
        AttentionChannels.cancel(app, id)
        changed()
    }

    /** Swiped away or put off with Later: once per card; it stays listed but no longer takes over on unlock. */
    fun dismissed(ctx: Context, id: String) {
        val app = ctx.applicationContext
        val remove = synchronized(lock) {
            ensure(app)
            val c = book[id] ?: return
            if (c.dismissed) return
            c.dismissed = true
            event("dismissed", JSONObject().put("id", id))
            // A local alert is only a heads-up: dismissing it is the end of it.
            if (c.local) { book.remove(id); outcomes[id] = c }
            saveCards(); c.local
        }
        if (remove) AttentionChannels.cancel(app, id)
        changed()
    }

    /** Displayed on screen: the card screen was visible, or the heads-up appeared while the phone was in use. Once per card. */
    fun seen(ctx: Context, id: String, via: String) = once(ctx, id, "seen", via) { it.seen = true }
    /** The user opened the card (tapped its notification or its row). Once per card. */
    fun opened(ctx: Context, id: String) = once(ctx, id, "opened", null) { it.opened = true }

    private fun once(ctx: Context, id: String, type: String, via: String?, mark: (AttentionCard) -> Unit) {
        synchronized(lock) {
            ensure(ctx.applicationContext)
            val c = book[id] ?: return
            if ((type == "seen" && c.seen) || (type == "opened" && c.opened)) return
            mark(c)
            event(type, JSONObject().put("id", id).apply { if (via != null) put("via", via) })
            saveCards()
        }
    }

    /** Close a failed card. */
    fun forget(ctx: Context, id: String) {
        synchronized(lock) { ensure(ctx.applicationContext); book.remove(id)?.let { outcomes[id] = it }; saveCards() }
        AttentionChannels.cancel(ctx.applicationContext, id)
        changed()
    }

    /**
     * ACTION_USER_PRESENT: record it, and an ordinary decision still waiting takes over the screen now.
     * A pending card whose notification is no longer in the shade (and was not swiped away: that
     * would be a dismissed event) is put back quietly, so a waiting decision is always visible.
     */
    fun unlocked(ctx: Context) {
        val app = ctx.applicationContext
        val (next, pending) = synchronized(lock) {
            ensure(app); expire(app)
            event("unlocked")
            book.takeovers(now()).firstOrNull() to book.showing(now()).filter { it.state == "pending" && !it.dismissed && !it.local }
        }
        changed()
        if (next != null && !AttentionCardActivity.onScreen) launchCard(app, next.id)
        val active = AttentionChannels.active(app) ?: return
        pending.filter { AttentionChannels.notificationId(it.id) !in active }.forEach {
            Log.i(TAG, "card ${it.id}: notification missing, posted again")
            AttentionChannels.postCard(app, it, ring = false, silent = true)
        }
    }

    private fun expire(app: Context) {
        val gone = book.expire(now())
        if (gone.isEmpty()) return
        gone.forEach { outcomes[it.id] = it.apply { state = "gone" }; AttentionChannels.cancel(app, it.id) }
        saveCards(); changed()
    }

    private fun show(app: Context, card: AttentionCard, ring: Boolean) {
        AttentionChannels.postCard(app, card, ring)
        if (!ring) return
        val inUse = screenInUse(app)
        // A full-screen intent only takes over a locked or sleeping phone; in use, it would be a heads-up.
        if (card.urgent && inUse && !AttentionCardActivity.onScreen) launchCard(app, card.id)
        else if (inUse && card.channel in setOf("approvals", "family", "urgent") && AttentionChannels.enabled(app, channelOf(card))) seen(app, card.id, "heads_up")
    }

    private fun channelOf(card: AttentionCard) = AttentionChannels.channelFor(card)

    fun screenInUse(ctx: Context): Boolean = try {
        (ctx.getSystemService(Context.POWER_SERVICE) as PowerManager).isInteractive &&
            !(ctx.getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager).isKeyguardLocked
    } catch (_: Exception) { false }

    /** Background activity starts need "Display over other apps" (or a bound accessibility service) on Android 10+. */
    fun launchCard(ctx: Context, id: String) {
        try {
            ctx.startActivity(Intent(ctx, AttentionCardActivity::class.java).putExtra(AttentionCardActivity.EXTRA_ID, id)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP))
        } catch (e: Exception) { Log.i(TAG, "card takeover not allowed: ${e.message}") }
    }

    // ---- notifications from other apps ----------------------------------------

    /** From [RookNotificationListener]: classify on the phone; only urgent/family summaries ever leave it. */
    fun observe(ctx: Context, sbn: StatusBarNotification) {
        val app = ctx.applicationContext
        if (sbn.packageName == app.packageName || sbn.isOngoing) return
        val n = sbn.notification ?: return
        if (n.flags and (Notification.FLAG_FOREGROUND_SERVICE or Notification.FLAG_GROUP_SUMMARY or Notification.FLAG_ONGOING_EVENT) != 0) return
        if (n.category in NOISE) return
        val ex = n.extras ?: return
        val title = ex.getCharSequence(Notification.EXTRA_TITLE)?.toString().orEmpty()
        val text = (ex.getCharSequence(Notification.EXTRA_BIG_TEXT) ?: ex.getCharSequence(Notification.EXTRA_TEXT))?.toString().orEmpty().take(2000)
        if (title.isBlank() && text.isBlank()) return
        val hash = (title + "\u0000" + text).hashCode()
        synchronized(lock) { if (lastContent[sbn.key] == hash) return; lastContent[sbn.key] = hash }
        val target = n.contentIntent
        worker.execute { runCatching { classify(app, sbn.packageName, title, text, target) }.onFailure { Log.w(TAG, "classify failed", it) } }
    }

    private fun classify(app: Context, pkg: String, title: String, text: String, target: PendingIntent?) {
        val label = appLabel(app, pkg)
        val names = starredNames(app)
        var alert: AttentionCard? = null
        var ring = false
        val verdict = synchronized(lock) {
            ensure(app)
            val v = rules.classify(AttentionRules.Item(pkg, label, title, text), names)
            val t = now()
            if (v.priority == AttentionRules.IGNORE) { quiet.ignore(t); saveFeed(); return@synchronized v }
            val summary = AttentionRules.summary(label, v.category, title, text)
            lastId = maxOf(System.currentTimeMillis(), lastId + 1)
            val id = "f" + java.lang.Long.toString(lastId, 36)
            quiet.add(JSONObject().put("id", id).put("ts", t).put("app", label).put("category", v.category)
                .put("priority", v.priority).put("summary", summary), t)
            saveFeed()
            if (v.priority == AttentionRules.URGENT || v.priority == AttentionRules.FAMILY) {
                var cardId = id
                if (v.priority == AttentionRules.URGENT) {
                    // One ringing alert per app at a time: a burst updates it instead of ringing again.
                    val open = book.showing(t).firstOrNull { it.local && it.state == "pending" && it.source == label && t - it.created < 600 }
                    val c = AttentionCard.fromPost(JSONObject().put("id", open?.id ?: "n$id").put("kind", "alert").put("line", summary)
                        .put("urgency", "urgent").put("channel", "urgent").put("actions", JSONArray(listOf("ack")))
                        .put("source", label).put("expires_at", t + 12 * 3600), open?.created ?: t, local = true)
                    ring = book.post(c); saveCards()
                    alert = book[c.id]; cardId = c.id
                    if (target != null) targets[c.id] = target
                }
                event("notice", JSONObject().put("id", cardId).put("app", label).put("category", v.category)
                    .put("priority", v.priority).put("summary", summary))
            }
            v
        }
        alert?.let { show(app, it, ring) }
        if (verdict.priority == AttentionRules.FAMILY) AttentionChannels.postFamily(app, pkg, "$label: ${title.ifBlank { verdict.category }}", text, target)
        if (verdict.priority != AttentionRules.IGNORE) changed()
    }

    private val NOISE = setOf(Notification.CATEGORY_ALARM, Notification.CATEGORY_CALL, Notification.CATEGORY_PROGRESS,
        Notification.CATEGORY_SERVICE, Notification.CATEGORY_SYSTEM, Notification.CATEGORY_TRANSPORT, Notification.CATEGORY_NAVIGATION,
        Notification.CATEGORY_STOPWATCH)

    private fun appLabel(ctx: Context, pkg: String): String = try {
        val pm = ctx.packageManager
        pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString()
    } catch (_: Exception) {
        // Package visibility can hide other apps' labels: fall back to a readable package segment.
        pkg.split('.').lastOrNull { it !in setOf("android", "app", "mobile", "client") }?.replaceFirstChar { it.uppercase() } ?: pkg
    }

    /** Starred contacts' names (refreshed every 30 minutes) when contacts are granted and the rules want them. */
    private fun starredNames(ctx: Context): List<String> {
        if (!synchronized(lock) { ensure(ctx); rules.familyStarred }) return emptyList()
        if (System.currentTimeMillis() - starredAt < 30 * 60_000L) return starred
        starredAt = System.currentTimeMillis()
        if (ctx.checkSelfPermission(Manifest.permission.READ_CONTACTS) != PackageManager.PERMISSION_GRANTED) { starred = emptyList(); return starred }
        starred = try {
            ctx.contentResolver.query(ContactsContract.Contacts.CONTENT_URI, arrayOf(ContactsContract.Contacts.DISPLAY_NAME),
                "${ContactsContract.Contacts.STARRED}=1", null, null)?.use { c ->
                buildList { while (c.moveToNext()) c.getString(0)?.trim()?.takeIf { it.length >= 2 }?.let { add(it) } }
            } ?: emptyList()
        } catch (_: Exception) { emptyList() }
        return starred
    }
}

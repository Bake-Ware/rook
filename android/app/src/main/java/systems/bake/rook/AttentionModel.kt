package systems.bake.rook

import org.json.JSONArray
import org.json.JSONObject

/**
 * Pure state behind attention.* (no Android types, unit-tested): decision cards,
 * the event log the hub polls, and the quiet feed of classified notifications.
 * [AttentionStore] owns one of each, persists them and drives the UI.
 */
class AttentionCard(
    val id: String, var kind: String, var line: String, var explain: String, var urgency: String,
    var channel: String, var actions: List<String>, var source: String, var expiresAt: Double,
    val created: Double, val local: Boolean = false,
    /** Short plain lines ("For: …", "Who: …", "I want to: …", "Risk: …") always shown under the title. */
    var body: String = "",
) {
    /** pending -> sent (a decision went out) -> done | failed; "gone" removes the card. */
    var state = "pending"
    var message = ""
    var decision = ""
    /** Put away without a decision (swiped or Later): no more takeovers on unlock. */
    var dismissed = false
    var seen = false
    var opened = false

    val urgent get() = urgency == "urgent" || channel == "urgent"
    /** Ordinary decisions take over the screen when the phone is next unlocked. */
    val takeover get() = !urgent && channel in TAKEOVER_CHANNELS && actions.any { it == "yes" || it == "no" }
    /**
     * A decision the hub queued (digest channel, with Yes/No): no pop-up or takeover, but a normal
     * notification in the shade and status bar, never the collapsed silent section.
     */
    val waiting get() = !urgent && channel == "digest" && actions.any { it == "yes" || it == "no" }
    fun expired(now: Double) = expiresAt > 0 && now >= expiresAt

    fun toJson(): JSONObject = JSONObject().put("id", id).put("kind", kind).put("line", line).put("body", body).put("explain", explain)
        .put("urgency", urgency).put("channel", channel).put("actions", JSONArray(actions)).put("source", source)
        .put("expires_at", expiresAt).put("created", created).put("local", local).put("state", state)
        .put("message", message).put("decision", decision).put("dismissed", dismissed).put("seen", seen).put("opened", opened)

    companion object {
        val KINDS = setOf("hold", "job", "card", "ask", "alert")
        val CHANNELS = setOf("urgent", "family", "approvals", "digest", "jobs")
        val ACTIONS = listOf("yes", "no", "explain", "ack")
        val TAKEOVER_CHANNELS = setOf("approvals", "family")
        const val LINE_MAX = 140
        const val EXPLAIN_MAX = 600
        const val BODY_MAX = 600
        const val BODY_LINES = 6

        /** Newlines kept: each line's whitespace collapsed, blank lines dropped, at most BODY_LINES / BODY_MAX, "…" where cut. */
        fun cleanBody(text: String): String {
            val lines = text.lines().map { it.trim().split(Regex("\\s+")).joinToString(" ") }.filter { it.isNotEmpty() }
            var cut = lines.size > BODY_LINES
            var out = lines.take(BODY_LINES).joinToString("\n")
            if (out.length > BODY_MAX) { out = out.take(BODY_MAX - 1); cut = true }
            return if (cut) out.trimEnd() + "…" else out
        }

        /** A card from attention.post args; clips text, rejects what the contract does not allow. */
        fun fromPost(o: JSONObject, now: Double, local: Boolean = false): AttentionCard {
            val id = o.optString("id").trim()
            require(id.isNotEmpty() && id.length <= 64) { "id must be 1-64 characters" }
            val kind = o.optString("kind", "card").ifBlank { "card" }
            require(kind in KINDS) { "kind must be one of ${KINDS.sorted()}" }
            val line = o.optString("line").trim()
            require(line.isNotEmpty()) { "line is required" }
            val urgency = o.optString("urgency", "normal").ifBlank { "normal" }
            require(urgency == "urgent" || urgency == "normal") { "urgency must be urgent or normal" }
            val channel = o.optString("channel", "approvals").ifBlank { "approvals" }
            require(channel in CHANNELS) { "channel must be one of ${CHANNELS.sorted()}" }
            val given = o.optJSONArray("actions")
            val actions = if (given == null) (if (kind == "alert") listOf("ack") else listOf("yes", "no", "explain"))
                else (0 until given.length()).map { given.optString(it) }
            require(actions.all { it in ACTIONS }) { "actions must be a subset of $ACTIONS" }
            return AttentionCard(id, kind, line.take(LINE_MAX), o.optString("explain").trim().take(EXPLAIN_MAX), urgency, channel,
                ACTIONS.filter { it in actions }.ifEmpty { listOf("ack") }, o.optString("source").take(80),
                o.optDouble("expires_at", 0.0).takeIf { it.isFinite() && it > 0 } ?: 0.0, now, local,
                cleanBody(o.optString("body")))
        }

        fun fromJson(o: JSONObject): AttentionCard = AttentionCard(o.getString("id"), o.optString("kind", "card"),
            o.optString("line"), o.optString("explain"), o.optString("urgency", "normal"), o.optString("channel", "approvals"),
            o.optJSONArray("actions")?.let { a -> (0 until a.length()).map { a.optString(it) } } ?: listOf("yes", "no"),
            o.optString("source"), o.optDouble("expires_at", 0.0), o.optDouble("created", 0.0), o.optBoolean("local"),
            o.optString("body")).apply {
            state = o.optString("state", "pending"); message = o.optString("message"); decision = o.optString("decision")
            dismissed = o.optBoolean("dismissed"); seen = o.optBoolean("seen"); opened = o.optBoolean("opened")
        }
    }
}

/** Cards by id; showing order is oldest first. */
class CardBook(private val cap: Int = 100) {
    private val cards = linkedMapOf<String, AttentionCard>()

    operator fun get(id: String) = cards[id]
    val size get() = cards.size

    /** Returns true for a new card (it may ring); a known id is updated in place and never re-rings. */
    fun post(card: AttentionCard): Boolean {
        val old = cards[card.id]
        if (old == null) {
            cards[card.id] = card
            while (cards.size > cap) cards.remove((cards.values.firstOrNull { it.state != "pending" } ?: cards.values.first()).id)
            return true
        }
        old.kind = card.kind; old.line = card.line; old.body = card.body; old.explain = card.explain; old.urgency = card.urgency
        old.channel = card.channel; old.actions = card.actions; old.source = card.source; old.expiresAt = card.expiresAt
        if (old.state == "failed") { old.state = "pending"; old.message = "" }
        return false
    }

    /** attention.update: done/gone remove the card (returned so the screen can show the outcome); failed keeps it with the message. */
    fun update(id: String, state: String, message: String): AttentionCard? {
        val c = cards[id] ?: return null
        c.state = state; c.message = message.take(300)
        if (state == "done" || state == "gone") cards.remove(id)
        return c
    }

    fun remove(id: String): AttentionCard? = cards.remove(id)

    /** Expired cards, removed. */
    fun expire(now: Double): List<AttentionCard> = cards.values.filter { it.expired(now) }.onEach { cards.remove(it.id) }

    /** What the card screen and the app list show, oldest first. */
    fun showing(now: Double): List<AttentionCard> = cards.values.filter { !it.expired(now) }.sortedBy { it.created }

    /** Cards that may take over the screen on unlock. */
    fun takeovers(now: Double): List<AttentionCard> = showing(now).filter { it.takeover && it.state == "pending" && !it.dismissed }

    fun toJson(): JSONArray = JSONArray().apply { cards.values.forEach { put(it.toJson()) } }
    fun load(a: JSONArray?) {
        cards.clear()
        for (i in 0 until (a?.length() ?: 0)) a?.optJSONObject(i)?.let { o ->
            runCatching { AttentionCard.fromJson(o) }.getOrNull()?.let { cards[it.id] = it }
        }
    }
}

/** Events for attention.poll: monotonic seq (survives pruning), capped. */
class EventLog(private val cap: Int = 1000) {
    var seq = 0L
        private set
    private val events = ArrayDeque<JSONObject>()
    val size get() = events.size

    fun add(type: String, fields: JSONObject = JSONObject(), now: Double): JSONObject {
        val e = JSONObject().put("seq", ++seq).put("ts", now).put("type", type)
        fields.keys().forEach { k -> e.put(k, fields.get(k)) }
        events.addLast(e)
        while (events.size > cap) events.removeFirst()
        return e
    }

    /**
     * Events after [since], at most [limit]. `last` is the seq to pass next time.
     * A [since] ahead of this log (the app's data was cleared) restarts from the beginning with reset=true.
     */
    fun poll(since: Long, limit: Int = 500): JSONObject {
        val reset = since > seq
        val from = if (reset) 0L else since
        val out = events.filter { it.getLong("seq") > from }
        val page = out.take(limit)
        val r = JSONObject().put("ok", true).put("events", JSONArray(page))
            .put("last", page.lastOrNull()?.getLong("seq") ?: if (reset) seq else since)
        if (reset) r.put("reset", true)
        // Older events were dropped by the cap before they were polled.
        if ((events.firstOrNull()?.getLong("seq") ?: 0L) > from + 1) r.put("gap", true)
        if (out.size > page.size) r.put("more", true)
        return r
    }

    fun toJson(): JSONObject = JSONObject().put("seq", seq).put("events", JSONArray(events.toList()))
    fun load(o: JSONObject?) {
        events.clear(); seq = o?.optLong("seq", 0L) ?: 0L
        val a = o?.optJSONArray("events")
        for (i in 0 until (a?.length() ?: 0)) a?.optJSONObject(i)?.let { events.addLast(it) }
        while (events.size > cap) events.removeFirst()
        seq = maxOf(seq, events.lastOrNull()?.optLong("seq") ?: 0L)
    }
}

/** The quiet feed: non-ignored notification summaries (newest last), capped and kept 7 days, plus a daily ignored count. */
class QuietFeed(private val cap: Int = 300, private val maxAgeSecs: Double = 7 * 86400.0) {
    private val entries = ArrayDeque<JSONObject>()
    private val ignored = sortedMapOf<Long, Int>()
    val size get() = entries.size

    fun add(entry: JSONObject, now: Double) {
        entries.addLast(entry.put("ts", entry.optDouble("ts", now)))
        prune(now)
    }

    fun ignore(now: Double) { val day = (now / 86400).toLong(); ignored[day] = (ignored[day] ?: 0) + 1; prune(now) }

    fun prune(now: Double) {
        while (entries.size > cap) entries.removeFirst()
        while (entries.isNotEmpty() && now - entries.first().optDouble("ts", now) > maxAgeSecs) entries.removeFirst()
        val oldest = ((now - maxAgeSecs) / 86400).toLong()
        ignored.keys.filter { it < oldest }.forEach { ignored.remove(it) }
    }

    fun recent(limit: Int): List<JSONObject> = entries.toList().asReversed().take(limit.coerceAtLeast(0))
    fun ignoredCount(): Int = ignored.values.sum()

    fun toJson(): JSONObject = JSONObject().put("entries", JSONArray(entries.toList()))
        .put("ignored", JSONObject().apply { ignored.forEach { (d, n) -> put(d.toString(), n) } })
    fun load(o: JSONObject?, now: Double) {
        entries.clear(); ignored.clear()
        val a = o?.optJSONArray("entries")
        for (i in 0 until (a?.length() ?: 0)) a?.optJSONObject(i)?.let { entries.addLast(it) }
        o?.optJSONObject("ignored")?.let { ig -> ig.keys().forEach { k -> k.toLongOrNull()?.let { ignored[it] = ig.optInt(k) } } }
        prune(now)
    }
}

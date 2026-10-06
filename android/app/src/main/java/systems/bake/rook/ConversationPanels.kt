package systems.bake.rook

import android.content.Context
import android.graphics.Color
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.view.View
import android.widget.LinearLayout
import android.widget.TextView
import com.google.android.material.tabs.TabLayout
import systems.bake.rook.databinding.ActivityMainBinding
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Activity-local presentation, separate from conversation bubbles and transport behavior. */
class ConversationPanels(private val ctx: Context, private val b: ActivityMainBinding) {
    private val timeline = ActivityTimeline()
    /** Shared with the service (outlives this screen); read directly, never replayed. */
    private val background get() = VoiceBus.background
    /** [BackgroundTimeline.version] this screen has counted toward its unread badge. */
    private var backgroundCounted = 0L
    /** Collapsible parts the user opened: "<connection>:<seq>:args|result". */
    private val expanded = mutableSetOf<String>()
    private val activityUnread = UnseenItems()
    private val backgroundUnread = UnseenItems()
    private val status = TurnStatus()
    private var connection = -1L
    private var turn = -1
    private var selected = 0
    private var hasActivity = false
    private var listeningRequested = false
    private val main = Handler(Looper.getMainLooper())
    private val tick = object : Runnable { override fun run() { renderStatus(); main.postDelayed(this, 1000) } }
    private val red = Color.rgb(239, 120, 120)
    private val amber = Color.rgb(216, 173, 109)
    private fun dp(n: Int) = (n * ctx.resources.displayMetrics.density).toInt()
    init {
        b.tabs.setTabTextColors(ctx.getColor(R.color.rook_dim), ctx.getColor(R.color.rook_accent))
        b.tabs.setSelectedTabIndicatorColor(ctx.getColor(R.color.rook_accent))
        listOf("Chat", "Activity", "Background").forEach { name ->
            b.tabs.addTab(b.tabs.newTab().setText(name))
        }
        // Material's default text appearance may uppercase labels: explicitly preserve sentence case.
        fun sentenceCase(view: View) {
            if (view is TextView) view.isAllCaps = false
            if (view is android.view.ViewGroup) for (i in 0 until view.childCount) sentenceCase(view.getChildAt(i))
        }
        sentenceCase(b.tabs)
        b.tabs.addOnTabSelectedListener(object : TabLayout.OnTabSelectedListener {
            override fun onTabSelected(tab: TabLayout.Tab) {
                selected = tab.position
                b.chatList.visibility = if (selected == 0) View.VISIBLE else View.GONE
                b.activityScroll.visibility = if (selected == 1) View.VISIBLE else View.GONE
                b.backgroundScroll.visibility = if (selected == 2) View.VISIBLE else View.GONE
                if (selected == 1) activityUnread.seen()
                if (selected == 2) { countBackground(); backgroundUnread.seen(); renderBackground() }
                badges()
            }
            override fun onTabUnselected(tab: TabLayout.Tab) {}
            override fun onTabReselected(tab: TabLayout.Tab) = onTabSelected(tab)
        })
        // The Background tab is first rendered by [resume], after the screen attaches.
    }
    fun sync(generation: Long) { if (generation != connection) { connection = generation; turn = -1; hasActivity = false; status.reset() } }
    fun turn(id: Int) { turn = id }
    fun begin(label: String) { status.begin(SystemClock.elapsedRealtime(), label); renderStatus() }
    fun listening() { listeningRequested = true; begin("Listening") }
    fun state(state: String) {
        status.legacyState(state, SystemClock.elapsedRealtime())
        if (state == "listening" && listeningRequested) begin("Listening")
        if (state in listOf("thinking", "speaking", "idle", "standby")) listeningRequested = false
        renderStatus()
    }
    fun done() { status.finishLegacy(); renderStatus() }
    fun interrupt() { listeningRequested = false; status.finish(); renderStatus() }
    /** After [VoiceBus.attach]: counts Background steps missed while away and renders the tab once. */
    fun resume() {
        sync(VoiceBus.connectionGeneration)
        countBackground()
        renderBackground(); badges()
        main.removeCallbacks(tick); main.post(tick)
    }
    fun pause() { main.removeCallbacks(tick) }
    fun activity(e: ActivityEvent) {
        if (!timeline.add(connection, e)) return
        hasActivity = true; listeningRequested = false; turn = maxOf(turn, e.turn)
        status.event(e, SystemClock.elapsedRealtime())
        activityUnread.add(e.failed, selected == 1)
        renderActivity(); renderStatus(); badges()
    }
    fun note(text: String, failed: Boolean = false) {
        timeline.local(connection, ActivityEvent(turn, -1, System.currentTimeMillis(), if (failed) "error" else "legacy",
            text, null, null, null, null, null, null, if (failed) "failed" else null))
        activityUnread.add(failed, selected == 1); renderActivity(); badges()
    }
    fun tool(title: String, state: String) { if (!hasActivity) note("$title · $state", state in listOf("error", "failed")) }
    /** A Background step was added to [VoiceBus.background]. */
    fun background(@Suppress("UNUSED_PARAMETER") e: BackgroundEvent) {
        if (expanded.size > 200) expanded.clear()
        countBackground()
        // A replay into a resuming screen renders once in [resume], not once per event.
        if (selected == 2 && !VoiceBus.replaying) renderBackground()
        badges()
    }
    private fun countBackground() {
        val missed = background.version - backgroundCounted
        if (missed > 0 && selected != 2) backgroundUnread.addMany(missed.coerceAtMost(Int.MAX_VALUE.toLong()).toInt(),
            background.lastFailure > backgroundCounted)
        backgroundCounted = background.version
    }
    private fun badges() {
        listOf(1 to activityUnread, 2 to backgroundUnread).forEach { (index, unseen) ->
            val tab = b.tabs.getTabAt(index) ?: return@forEach
            if (unseen.count == 0) tab.removeBadge()
            else tab.orCreateBadge.apply { number = unseen.count; backgroundColor = if (unseen.failed) red else amber
                badgeTextColor = ctx.getColor(R.color.rook_bg) }
        }
    }
    private fun text(parent: LinearLayout, value: String, color: Int = ctx.getColor(R.color.rook_fg), size: Float = 13f) {
        parent.addView(TextView(ctx).apply { text = value; textSize = size; setTextColor(color); setPadding(0, dp(4), 0, dp(4)) })
    }
    private fun card(parent: LinearLayout): LinearLayout = LinearLayout(ctx).apply {
        orientation = LinearLayout.VERTICAL; setPadding(dp(12), dp(8), dp(12), dp(8)); setBackgroundResource(R.drawable.rook_panel)
        parent.addView(this, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(8) })
    }
    private fun title(key: TurnKey) = if (key.turn < 0) "Session ${key.connection}" else "Turn ${key.turn} · session ${key.connection}"
    private fun renderActivity() {
        b.activityList.removeAllViews()
        val format = SimpleDateFormat("HH:mm:ss", Locale.getDefault())
        for ((key, events) in timeline.turns) {
            val group = card(b.activityList); text(group, title(key), amber)
            for (e in events) {
                text(group, "${format.format(Date(e.ts))}  ${e.icon}  ${e.label}" + (e.elapsedMs?.let { " · $it ms" } ?: ""), if (e.failed) red else ctx.getColor(R.color.rook_fg))
                val detail = listOfNotNull(e.detail, listOfNotNull(e.worker, e.cap ?: e.tool).joinToString(" ").takeIf { it.isNotEmpty() }, e.timeoutMs?.let { "Timeout: $it ms" }, e.status).joinToString(" · ")
                if (detail.isNotEmpty()) text(group, detail, ctx.getColor(R.color.rook_dim), 12f)
            }
        }
        b.activityScroll.post { b.activityScroll.fullScroll(View.FOCUS_DOWN) }
    }
    /** A tappable "label ▸" line that shows [body] under it while expanded. */
    private fun collapsible(parent: LinearLayout, key: String, label: String, body: String) {
        val open = key in expanded
        val head = TextView(ctx).apply {
            text = "$label ${if (open) "▾" else "▸"}"; textSize = 12f; setTextColor(amber)
            setPadding(dp(12), dp(2), 0, dp(2)); isFocusable = true
            contentDescription = "$label, ${if (open) "expanded" else "collapsed"}. Tap to ${if (open) "hide" else "show"}"
            setOnClickListener { if (!expanded.remove(key)) expanded.add(key); renderBackground(scroll = false) }
        }
        parent.addView(head)
        if (open) parent.addView(TextView(ctx).apply {
            text = body; textSize = 12f; typeface = android.graphics.Typeface.MONOSPACE; setTextIsSelectable(true)
            setTextColor(ctx.getColor(R.color.rook_dim)); setPadding(dp(12), dp(2), 0, dp(6))
        })
    }
    private fun renderBackground(scroll: Boolean = true) {
        b.backgroundList.removeAllViews()
        if (background.turns.isEmpty()) {
            text(card(b.backgroundList), "Background work appears here: what was looked up or done for each turn, step by step.", ctx.getColor(R.color.rook_dim))
            return
        }
        val format = SimpleDateFormat("HH:mm:ss", Locale.getDefault())
        for ((key, events) in background.turns) {
            val group = card(b.backgroundList)
            text(group, title(key) + if (background.running(key)) " · running" else "", amber)
            background.truncated[key]?.let { text(group, "… $it earlier steps not shown", ctx.getColor(R.color.rook_dim), 12f) }
            for (e in events) {
                val line = listOfNotNull("${format.format(Date(e.ts))}  ${e.icon}  ${e.text}", e.tool,
                    e.status?.takeIf { it != "ok" }, e.elapsedMs?.let { "$it ms" }).joinToString(" · ")
                text(group, line, if (e.failed) red else ctx.getColor(R.color.rook_fg))
                e.args?.let { collapsible(group, "${key.connection}:${e.seq}:args", "args", it) }
                e.result?.let { collapsible(group, "${key.connection}:${e.seq}:result", "result", it) }
            }
        }
        if (scroll) b.backgroundScroll.post { b.backgroundScroll.fullScroll(View.FOCUS_DOWN) }
    }
    private fun renderStatus() {
        val d = status.display(SystemClock.elapsedRealtime())
        b.turnStatus.visibility = if (d == null) View.GONE else View.VISIBLE
        d ?: return
        b.turnStatus.text = "${if (d.severity > 0) "⚠" else "◷"} ${d.text} · ${d.seconds}s"
        b.turnStatus.setTextColor(when (d.severity) { 2 -> red; 1 -> amber; else -> ctx.getColor(R.color.rook_dim) })
    }
}

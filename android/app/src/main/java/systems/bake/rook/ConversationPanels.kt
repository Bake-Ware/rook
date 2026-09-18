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
class ConversationPanels(private val ctx: Context, private val b: ActivityMainBinding, private val openDecision: (TurnKey) -> Unit) {
    private val timeline = ActivityTimeline()
    private val decisions = linkedMapOf<TurnKey, Decision>()
    private val activityUnread = UnseenItems()
    private val decisionUnread = UnseenItems()
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
    private fun thinking() = ctx.getSharedPreferences("rook", Context.MODE_PRIVATE).getBoolean("show_thinking", false)
    private fun dp(n: Int) = (n * ctx.resources.displayMetrics.density).toInt()
    init {
        b.tabs.setTabTextColors(ctx.getColor(R.color.rook_dim), ctx.getColor(R.color.rook_accent))
        b.tabs.setSelectedTabIndicatorColor(ctx.getColor(R.color.rook_accent))
        listOf("Chat", "Activity", "Decisions").forEach { name ->
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
                b.decisionsScroll.visibility = if (selected == 2) View.VISIBLE else View.GONE
                if (selected == 1) activityUnread.seen()
                if (selected == 2) { decisionUnread.seen(); renderDecisions() }
                badges()
            }
            override fun onTabUnselected(tab: TabLayout.Tab) {}
            override fun onTabReselected(tab: TabLayout.Tab) = onTabSelected(tab)
        })
        renderDecisions()
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
    fun resume() { sync(VoiceBus.connectionGeneration); renderDecisions(); main.removeCallbacks(tick); main.post(tick) }
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
    fun decision(d: Decision) {
        if (!thinking()) return
        val key = TurnKey(connection, d.turn)
        if (decisions[key] == d) return
        decisions[key] = d
        decisionUnread.add(d.failed, selected == 2); renderDecisions(); badges()
    }
    private fun badges() {
        listOf(1 to activityUnread, 2 to decisionUnread).forEach { (index, unseen) ->
            val tab = b.tabs.getTabAt(index) ?: return@forEach
            if (unseen.count == 0 || (index == 2 && !thinking())) tab.removeBadge()
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
    private fun renderDecisions() {
        b.decisionsList.removeAllViews()
        if (!thinking()) {
            text(card(b.decisionsList), "Turn on Show thinking in settings to see decisions")
            badges(); return
        }
        if (decisions.isEmpty()) text(card(b.decisionsList), "Decisions will appear here when the server sends them.")
        for ((key, d) in decisions) {
            val group = card(b.decisionsList)
            text(group, "${title(key)}  ·  ${d.mode.ifBlank { "shadow" }}", amber)
            DecisionContent.fill(group, d)
            group.contentDescription = "${title(key)}, ${d.engineLabel}. Show decision in chat"
            group.isFocusable = true
            group.setOnClickListener { b.tabs.getTabAt(0)?.select(); openDecision(key) }
        }
        b.decisionsScroll.post { b.decisionsScroll.fullScroll(View.FOCUS_DOWN) }
    }
    private fun renderStatus() {
        val d = status.display(SystemClock.elapsedRealtime())
        b.turnStatus.visibility = if (d == null) View.GONE else View.VISIBLE
        d ?: return
        b.turnStatus.text = "${if (d.severity > 0) "⚠" else "◷"} ${d.text} · ${d.seconds}s"
        b.turnStatus.setTextColor(when (d.severity) { 2 -> red; 1 -> amber; else -> ctx.getColor(R.color.rook_dim) })
    }
}

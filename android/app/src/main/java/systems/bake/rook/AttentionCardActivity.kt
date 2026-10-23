package systems.bake.rook

import android.app.KeyguardManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.os.Build
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity

/**
 * The decision card: one line in large text, big Yes / No / Explain (or Got it), source and age
 * small, "1 of N" when several wait (oldest first). Urgent cards show over the lock screen and
 * turn the screen on; Yes/No on a locked phone asks for the unlock first. A decision shows
 * "Sent…" until the hub's attention.update says done (brief confirmation) or failed.
 */
class AttentionCardActivity : AppCompatActivity() {

    private var wanted: String? = null
    private var current: AttentionCard? = null
    private var explainOpen = false
    private var reasonOpen = false
    private var leaving: Runnable? = null
    private val refresh = Runnable { render() }

    private lateinit var meta: TextView
    private lateinit var count: TextView
    private lateinit var line: TextView
    private lateinit var explain: TextView
    private lateinit var status: TextView
    private lateinit var reason: EditText
    private lateinit var reasonRow: LinearLayout
    private lateinit var buttons: LinearLayout
    private lateinit var later: TextView

    private fun dp(n: Int) = (n * resources.displayMetrics.density).toInt()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val fg = getColor(R.color.rook_fg); val dim = getColor(R.color.rook_dim)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL; gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(24), dp(32), dp(24), dp(24)); setBackgroundColor(getColor(R.color.rook_bg))
        }
        val top = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        meta = TextView(this).apply { textSize = 13f; setTextColor(dim) }
        count = TextView(this).apply { textSize = 13f; setTextColor(dim) }
        top.addView(meta, LinearLayout.LayoutParams(0, -2, 1f)); top.addView(count)
        root.addView(top)
        line = TextView(this).apply { textSize = 28f; setTextColor(fg); setPadding(0, dp(20), 0, dp(12)); setLineSpacing(0f, 1.1f) }
        root.addView(line)
        explain = TextView(this).apply { textSize = 17f; setTextColor(fg); setPadding(0, 0, 0, dp(12)); visibility = View.GONE }
        root.addView(explain)
        status = TextView(this).apply { textSize = 17f; setPadding(0, dp(8), 0, dp(8)); visibility = View.GONE }
        root.addView(status)
        reasonRow = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; visibility = View.GONE }
        reason = EditText(this).apply { hint = "Why not? (optional)"; isSingleLine = true; maxLines = 1 }
        reasonRow.addView(reason, LinearLayout.LayoutParams(-1, -2))
        val reasonButtons = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        reasonButtons.addView(bigButton("Send No") { deny(reason.text.toString()) }, weighted())
        reasonButtons.addView(bigButton("Skip", outlined = true) { deny("") }, weighted())
        reasonRow.addView(reasonButtons)
        root.addView(reasonRow)
        buttons = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; setPadding(0, dp(12), 0, 0) }
        root.addView(buttons)
        later = TextView(this).apply {
            text = "Later"; textSize = 15f; setTextColor(dim); gravity = Gravity.CENTER
            setPadding(0, dp(20), 0, dp(8)); setOnClickListener { later() }
        }
        root.addView(later, LinearLayout.LayoutParams(-1, -2))
        setContentView(ScrollView(this).apply { isFillViewport = true; addView(root, -1, -2) })
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) { override fun handleOnBackPressed() = later() })
        take(intent)
    }

    override fun onNewIntent(intent: Intent) { super.onNewIntent(intent); setIntent(intent); take(intent) }

    private fun take(i: Intent?) {
        val id = i?.getStringExtra(EXTRA_ID) ?: return
        wanted = id
        if (i.getBooleanExtra(EXTRA_OPENED, false)) AttentionStore.opened(this, id)
        if (current?.id != id) { explainOpen = false; reasonOpen = false }
        if (::line.isInitialized) render()
    }

    override fun onResume() { super.onResume(); onScreen = true; AttentionStore.addListener(refresh); render() }
    override fun onPause() { onScreen = false; AttentionStore.removeListener(refresh); super.onPause() }
    override fun onDestroy() { leaving?.let { line.removeCallbacks(it) }; super.onDestroy() }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) current?.let { AttentionStore.seen(this, it.id, "card") }
    }

    private fun render() {
        if (leaving != null) return
        val list = AttentionStore.showing(this)
        val cur = current
        if (cur != null && list.none { it.id == cur.id }) {
            val out = AttentionStore.outcome(cur.id)
            current = null
            if (wanted == cur.id) wanted = null
            if (out?.state == "done" && out.decision.isNotEmpty()) {
                // Brief confirmation, then the next card (or close).
                showStatus("Done ✓", ok = true); buttons.removeAllViews(); reasonRow.visibility = View.GONE
                leaving = Runnable { leaving = null; render() }.also { line.postDelayed(it, 1200) }
                return
            }
        }
        val pick = list.firstOrNull { it.id == wanted } ?: list.firstOrNull { it.id == cur?.id }
            ?: list.firstOrNull { !it.dismissed } ?: list.firstOrNull()
        if (pick == null) { finish(); return }
        if (pick.id != cur?.id) { explainOpen = false; reasonOpen = false; reason.text.clear() }
        current = pick
        bind(pick, list.indexOf(pick) + 1, list.size)
        if (hasWindowFocus()) AttentionStore.seen(this, pick.id, "card")
    }

    private fun bind(c: AttentionCard, index: Int, total: Int) {
        overLock(c.urgent)
        meta.text = listOf(c.source.ifBlank { "Rook" }, age(c.created)).joinToString(" · ")
        count.text = if (total > 1) "$index of $total" else ""
        line.text = c.line
        explain.text = c.explain
        explain.visibility = if (explainOpen && c.explain.isNotBlank()) View.VISIBLE else View.GONE
        reasonRow.visibility = if (reasonOpen && c.state == "pending") View.VISIBLE else View.GONE
        buttons.removeAllViews()
        when (c.state) {
            "sent" -> showStatus("Sent…")
            "failed" -> {
                showStatus("Didn’t go through" + if (c.message.isNotBlank()) ": ${c.message}" else "", failed = true)
                buttons.addView(bigButton("Close") { AttentionStore.forget(this, c.id) }, weighted())
            }
            else -> {
                status.visibility = View.GONE
                if (!reasonOpen) {
                    if ("yes" in c.actions) buttons.addView(bigButton("Yes") { whenUnlocked { AttentionStore.decide(this, c.id, true) } }, weighted())
                    if ("no" in c.actions) buttons.addView(bigButton("No", outlined = true) { reasonOpen = true; render(); reason.requestFocus() }, weighted())
                    if ("explain" in c.actions && c.explain.isNotBlank())
                        buttons.addView(bigButton(if (explainOpen) "Less" else "Explain", outlined = true) { explainOpen = !explainOpen; render() }, weighted())
                    if ("ack" in c.actions) buttons.addView(bigButton("Got it") { AttentionStore.ack(this, c.id) }, weighted())
                    AttentionStore.target(c.id)?.let { t ->
                        buttons.addView(bigButton("Open", outlined = true) {
                            whenUnlocked { runCatching { t.send() }; AttentionStore.ack(this, c.id) }
                        }, weighted())
                    }
                }
            }
        }
    }

    private fun deny(why: String) {
        val c = current ?: return
        whenUnlocked { reasonOpen = false; AttentionStore.decide(this, c.id, false, why) }
    }

    private fun later() {
        current?.let { if (it.state == "pending") AttentionStore.dismissed(this, it.id) }
        finish()
    }

    /** Yes/No from the lock screen asks for the unlock first; nobody approves anything from a locked phone. */
    private fun whenUnlocked(then: () -> Unit) {
        val km = getSystemService(KeyguardManager::class.java)
        if (Build.VERSION.SDK_INT >= 26 && km != null && km.isKeyguardLocked) {
            km.requestDismissKeyguard(this, object : KeyguardManager.KeyguardDismissCallback() {
                override fun onDismissSucceeded() { then() }
            })
        } else then()
    }

    private fun overLock(on: Boolean) {
        if (Build.VERSION.SDK_INT >= 27) { setShowWhenLocked(on); setTurnScreenOn(on); return }
        @Suppress("DEPRECATION")
        val f = WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
        if (on) window.addFlags(f) else window.clearFlags(f)
    }

    private fun showStatus(text: String, failed: Boolean = false, ok: Boolean = false) {
        status.text = text; status.visibility = View.VISIBLE
        status.setTextColor(when { failed -> Color.rgb(239, 120, 120); ok -> Color.rgb(164, 188, 146); else -> getColor(R.color.rook_dim) })
    }

    private fun weighted() = LinearLayout.LayoutParams(0, dp(64), 1f).apply { marginEnd = dp(8) }

    private fun bigButton(label: String, outlined: Boolean = false, onClick: () -> Unit): Button =
        (if (outlined) com.google.android.material.button.MaterialButton(this, null, com.google.android.material.R.attr.materialButtonOutlinedStyle)
         else com.google.android.material.button.MaterialButton(this)).apply {
            text = label; textSize = 19f; isAllCaps = false; setOnClickListener { onClick() }
        }

    private fun age(created: Double): String {
        val s = (System.currentTimeMillis() / 1000.0 - created).toLong().coerceAtLeast(0)
        return when { s < 60 -> "just now"; s < 3600 -> "${s / 60} min ago"; s < 86400 -> "${s / 3600} h ago"; else -> "${s / 86400} d ago" }
    }

    companion object {
        const val EXTRA_ID = "attention_id"
        const val EXTRA_OPENED = "attention_opened"
        /** True while a card is on screen: unlock takeovers don't stack a second one. */
        @Volatile var onScreen = false
    }
}

/** Swiped-away card notifications become "dismissed" events. */
class AttentionReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (intent.action != ACTION_DISMISSED) return
        intent.getStringExtra(AttentionCardActivity.EXTRA_ID)?.let { AttentionStore.dismissed(ctx, it) }
    }
    companion object { const val ACTION_DISMISSED = "systems.bake.rook.ATTENTION_DISMISSED" }
}

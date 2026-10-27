package systems.bake.rook

import android.app.KeyguardManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.graphics.Typeface
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.text.Layout
import android.text.SpannableString
import android.text.Spanned
import android.text.method.LinkMovementMethod
import android.text.style.StyleSpan
import android.text.util.Linkify
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.core.widget.TextViewCompat
import androidx.recyclerview.widget.RecyclerView
import androidx.viewpager2.widget.ViewPager2

/**
 * The decision cards: one page per card, swiped left and right freely (a swipe never answers),
 * "2 of 5" at the top. Each page: source and age small, a plain title, the always-shown lines
 * ("For:", "Who:", "I want to:", "Why:", "Risk:"), "More details" (the full why, the undo, the
 * exact call, where it was asked, links) and big Yes / No (or Got it; or, for a question with
 * choices, one button per option, the tapped option sent back word for word). An answer is instant: the
 * page says so, the card leaves the stack and the next one shows; the hub reads the decision on
 * its next poll in the background. If the hub refuses it, the card comes back with the reason;
 * if it never answers, the card comes back after a few minutes. Urgent cards show over the lock
 * screen; Yes/No on a locked phone asks for the unlock first.
 */
class AttentionCardActivity : AppCompatActivity() {

    private var wanted: String? = null
    private var cards: List<AttentionCard> = emptyList()
    private var currentId: String? = null
    private val detailsOpen = mutableSetOf<String>()
    private var reasonFor: String? = null
    /** A tap on an answer only counts once the pager has stood still this long (no answer from a fling). */
    private var settledAt = 0L
    /** While an answer's confirmation shows, the stack waits before moving on. */
    private var holdUntil = 0L
    private val refresh = Runnable { render() }
    private val clearFlash = Runnable { flash.visibility = View.GONE }

    private lateinit var count: TextView
    private lateinit var flash: TextView
    private lateinit var pager: ViewPager2
    private val adapter = Pages()

    private fun dp(n: Int) = (n * resources.displayMetrics.density).toInt()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val dim = getColor(R.color.rook_dim)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dp(16), 0, dp(8)); setBackgroundColor(getColor(R.color.rook_bg))
        }
        count = TextView(this).apply { textSize = 14f; setTextColor(dim); gravity = Gravity.CENTER; setPadding(dp(24), 0, dp(24), 0) }
        root.addView(count, LinearLayout.LayoutParams(-1, -2))
        flash = TextView(this).apply {
            textSize = 15f; gravity = Gravity.CENTER; setPadding(dp(24), dp(6), dp(24), 0); visibility = View.GONE
        }
        root.addView(flash, LinearLayout.LayoutParams(-1, -2))
        pager = ViewPager2(this).apply {
            adapter = this@AttentionCardActivity.adapter
            offscreenPageLimit = 1
            registerOnPageChangeCallback(object : ViewPager2.OnPageChangeCallback() {
                override fun onPageSelected(position: Int) {
                    val c = cards.getOrNull(position) ?: return
                    if (c.id != currentId) reasonFor = null
                    currentId = c.id
                    overLock(c.urgent)
                    showCount()
                    if (hasWindowFocus()) AttentionStore.seen(this@AttentionCardActivity, c.id, "card")
                }
                override fun onPageScrollStateChanged(state: Int) {
                    if (state == ViewPager2.SCROLL_STATE_IDLE) settledAt = SystemClock.uptimeMillis()
                }
            })
        }
        root.addView(pager, LinearLayout.LayoutParams(-1, 0, 1f))
        root.addView(TextView(this).apply {
            text = "Later"; textSize = 15f; setTextColor(dim); gravity = Gravity.CENTER
            setPadding(0, dp(12), 0, dp(12)); setOnClickListener { later() }
        }, LinearLayout.LayoutParams(-1, -2))
        setContentView(root)
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) { override fun handleOnBackPressed() = later() })
        take(intent)
    }

    override fun onNewIntent(intent: Intent) { super.onNewIntent(intent); setIntent(intent); take(intent) }

    private fun take(i: Intent?) {
        val id = i?.getStringExtra(EXTRA_ID) ?: return
        wanted = id
        if (i.getBooleanExtra(EXTRA_OPENED, false)) AttentionStore.opened(this, id)
        if (::pager.isInitialized) render()
    }

    override fun onResume() { super.onResume(); onScreen = true; AttentionStore.addListener(refresh); render() }
    override fun onPause() { onScreen = false; AttentionStore.removeListener(refresh); super.onPause() }
    override fun onDestroy() { pager.removeCallbacks(refresh); flash.removeCallbacks(clearFlash); super.onDestroy() }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) currentId?.let { AttentionStore.seen(this, it, "card") }
    }

    /** The stack again: answered cards out, failed ones back; the page stays on the card it showed, or the next one. */
    private fun render() {
        val wait = holdUntil - SystemClock.uptimeMillis()
        if (wait > 0) { pager.removeCallbacks(refresh); pager.postDelayed(refresh, wait); return }
        val old = cards
        val list = AttentionStore.stack(this)
        val failedNow = list.filter { c -> c.state == "failed" && old.none { it.id == c.id && it.state == "failed" } }
        if (failedNow.isNotEmpty() && old.isNotEmpty()) showFlash("Didn’t go through: ${failedNow.first().line}", failed = true)
        val at = pager.currentItem
        val keep = wanted?.let { w -> list.indexOfFirst { it.id == w }.takeIf { it >= 0 } }
            ?: currentId?.let { id -> list.indexOfFirst { it.id == id }.takeIf { it >= 0 } }
            // The card shown was answered (or withdrawn): the one after it, now in its place.
            ?: old.drop(at).firstNotNullOfOrNull { o -> list.indexOfFirst { it.id == o.id }.takeIf { it >= 0 } }
            ?: (list.size - 1)
        wanted = null
        if (list.isEmpty()) { finish(); return }
        cards = list
        // A card back in the stack (refused, or never confirmed) can be answered again.
        adapter.answered.keys.retainAll { id -> list.none { it.id == id } }
        adapter.notifyDataSetChanged()
        if (pager.currentItem != keep) pager.setCurrentItem(keep, false)
        // Another card under the finger: a tap that was aimed at the old one is not an answer.
        if (list[keep].id != currentId) settledAt = SystemClock.uptimeMillis()
        currentId = list[keep].id
        overLock(list[keep].urgent)
        showCount()
        if (hasWindowFocus()) AttentionStore.seen(this, list[keep].id, "card")
    }

    private fun showCount() {
        val i = pager.currentItem
        count.text = if (cards.size > 1) "${i + 1} of ${cards.size}" else ""
        count.visibility = if (cards.size > 1) View.VISIBLE else View.GONE
    }

    private fun showFlash(text: String, failed: Boolean = false) {
        flash.text = text; flash.visibility = View.VISIBLE
        flash.setTextColor(if (failed) Color.rgb(239, 120, 120) else Color.rgb(164, 188, 146))
        flash.removeCallbacks(clearFlash); flash.postDelayed(clearFlash, 3000)
    }

    /** Taps that come with or right after a swipe are not answers. */
    private fun steady(): Boolean =
        pager.scrollState == ViewPager2.SCROLL_STATE_IDLE && SystemClock.uptimeMillis() - settledAt >= SETTLE_MS

    /** Yes or No: recorded now, confirmed on the page for a moment, then the next card. */
    private fun answer(c: AttentionCard, approve: Boolean, why: String = "") {
        if (!steady() || cards.none { it.id == c.id }) return
        whenUnlocked {
            AttentionStore.decide(this, c.id, approve, why)
            reasonFor = null
            holdUntil = SystemClock.uptimeMillis() + CONFIRM_MS
            adapter.answered[c.id] = if (approve) "Yes ✓" else "No ✓"
            adapter.notifyItemChanged(cards.indexOfFirst { it.id == c.id }.coerceAtLeast(0))
            showFlash("${if (approve) "Yes" else "No"} sent: ${c.line}")
            pager.removeCallbacks(refresh); pager.postDelayed(refresh, CONFIRM_MS)
        }
    }

    /** One of a question's options: sent back verbatim, confirmed on the page, then the next card. */
    private fun choose(c: AttentionCard, option: String) {
        if (!steady() || cards.none { it.id == c.id } || c.chosen(option) == null) return
        whenUnlocked {
            AttentionStore.choose(this, c.id, option)
            reasonFor = null
            holdUntil = SystemClock.uptimeMillis() + CONFIRM_MS
            adapter.answered[c.id] = "“$option” ✓"
            adapter.notifyItemChanged(cards.indexOfFirst { it.id == c.id }.coerceAtLeast(0))
            showFlash("Sent “$option”: ${c.line}")
            pager.removeCallbacks(refresh); pager.postDelayed(refresh, CONFIRM_MS)
        }
    }

    private fun later() {
        cards.firstOrNull { it.id == currentId }?.let { if (it.state == "pending") AttentionStore.dismissed(this, it.id) }
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

    // ---- one page per card -------------------------------------------------------

    private inner class Page(val view: LinearLayout) : RecyclerView.ViewHolder(view) {
        val meta = text(13f, dim = true)
        val line = text(24f).apply { setLineSpacing(0f, 1.1f); setPadding(0, dp(12), 0, dp(12)) }
        val body = text(17f).apply { setLineSpacing(0f, 1.15f) }
        val more = text(16f).apply {
            setTextColor(getColor(R.color.rook_accent)); setPadding(0, dp(14), 0, dp(14)); minHeight = dp(48)
        }
        val details = text(16f).apply {
            setLineSpacing(0f, 1.15f); autoLinkMask = Linkify.WEB_URLS; movementMethod = LinkMovementMethod.getInstance()
            setLinkTextColor(getColor(R.color.rook_accent))
        }
        val status = text(17f).apply { setPadding(0, dp(8), 0, dp(4)) }
        val reason = EditText(this@AttentionCardActivity).apply { hint = "Why not? (optional)"; isSingleLine = true; maxLines = 1 }
        val reasonRow = LinearLayout(this@AttentionCardActivity).apply { orientation = LinearLayout.VERTICAL }
        val reasonButtons = row()
        /** A question's choices, one full-width button each, above the other buttons. */
        val choices = LinearLayout(this@AttentionCardActivity).apply { orientation = LinearLayout.VERTICAL }
        val buttons = row()

        init {
            view.orientation = LinearLayout.VERTICAL
            view.setPadding(dp(24), dp(8), dp(24), 0)
            val column = LinearLayout(this@AttentionCardActivity).apply { orientation = LinearLayout.VERTICAL }
            listOf(meta, line, body, more, details).forEach { column.addView(it, LinearLayout.LayoutParams(-1, -2)) }
            view.addView(ScrollView(this@AttentionCardActivity).apply { addView(column, -1, -2) }, LinearLayout.LayoutParams(-1, 0, 1f))
            view.addView(status, LinearLayout.LayoutParams(-1, -2))
            reasonRow.addView(reason, LinearLayout.LayoutParams(-1, -2))
            reasonRow.addView(reasonButtons, LinearLayout.LayoutParams(-1, -2))
            view.addView(reasonRow, LinearLayout.LayoutParams(-1, -2))
            view.addView(choices, LinearLayout.LayoutParams(-1, -2))
            view.addView(buttons, LinearLayout.LayoutParams(-1, -2))
        }

        fun bind(c: AttentionCard) {
            val now = System.currentTimeMillis() / 1000.0
            meta.text = listOf(c.source.ifBlank { "Rook" }, age(c.created)).joinToString(" · ")
            line.text = c.line
            line.setTextSize(TypedValue.COMPLEX_UNIT_SP, if (c.body.isNotBlank()) 22f else 26f)
            body.text = labelled(c.body)
            body.visibility = if (c.body.isNotBlank()) View.VISIBLE else View.GONE
            val open = c.id in detailsOpen
            val hasDetails = c.explain.isNotBlank() && "explain" in c.actions
            more.text = if (open) "Less" else "More details"
            more.visibility = if (hasDetails) View.VISIBLE else View.GONE
            more.setOnClickListener { if (!detailsOpen.remove(c.id)) detailsOpen.add(c.id); bind(c) }
            details.text = labelled(c.explain)
            details.visibility = if (open && hasDetails) View.VISIBLE else View.GONE
            buttons.removeAllViews(); reasonButtons.removeAllViews(); choices.removeAllViews()
            val done = adapter.answered[c.id]
            reasonRow.visibility = if (reasonFor == c.id && done == null) View.VISIBLE else View.GONE
            when {
                done != null -> status(done, ok = true)
                c.state == "failed" -> status("Didn’t go through" + if (c.message.isNotBlank()) ": ${c.message}" else "", failed = true)
                c.unconfirmed(now) -> status("The hub hasn’t confirmed your answer yet. Answer again only if it didn’t happen.")
                else -> status.visibility = View.GONE
            }
            if (done != null) return
            if (reasonFor == c.id) {
                reasonButtons.addView(bigButton("Send No") { answer(c, false, reason.text.toString()) }, weighted())
                reasonButtons.addView(bigButton("Back", outlined = true) { reasonFor = null; bind(c) }, weighted())
                return
            }
            reason.text.clear()
            if (c.choosing) c.options.forEach { option ->
                choices.addView(choiceButton(option) { choose(c, option) },
                    LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(6); marginStart = dp(4); marginEnd = dp(4) })
            }
            if ("yes" in c.actions) buttons.addView(bigButton("Yes") { answer(c, true) }, weighted())
            if ("no" in c.actions) buttons.addView(bigButton("No", outlined = true) {
                if (steady()) { reasonFor = c.id; bind(c); reason.requestFocus() }
            }, weighted())
            if ("ack" in c.actions) buttons.addView(bigButton("Got it") { if (steady()) AttentionStore.ack(this@AttentionCardActivity, c.id) }, weighted())
            AttentionStore.target(c.id)?.let { t ->
                buttons.addView(bigButton("Open", outlined = true) {
                    if (steady()) whenUnlocked { runCatching { t.send() }; AttentionStore.ack(this@AttentionCardActivity, c.id) }
                }, weighted())
            }
            if (c.state == "failed") buttons.addView(bigButton("Close", outlined = true) {
                if (steady()) AttentionStore.forget(this@AttentionCardActivity, c.id)
            }, weighted())
        }

        private fun status(text: String, failed: Boolean = false, ok: Boolean = false) {
            status.text = text; status.visibility = View.VISIBLE
            status.setTextColor(when { failed -> Color.rgb(239, 120, 120); ok -> Color.rgb(164, 188, 146); else -> getColor(R.color.rook_dim) })
        }
    }

    private inner class Pages : RecyclerView.Adapter<Page>() {
        /** Cards answered on this screen, shown as such until the stack moves on. */
        val answered = mutableMapOf<String, String>()
        init { setHasStableIds(true) }
        override fun getItemCount() = cards.size
        override fun getItemId(position: Int) = cards[position].id.hashCode().toLong()
        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int) =
            Page(LinearLayout(parent.context).apply { layoutParams = ViewGroup.LayoutParams(-1, -1) })
        override fun onBindViewHolder(holder: Page, position: Int) = holder.bind(cards[position])
    }

    /** Wraps at words, never mid-word with a hyphen, at any font size. */
    private fun text(size: Float, dim: Boolean = false) = TextView(this).apply {
        textSize = size; setTextColor(getColor(if (dim) R.color.rook_dim else R.color.rook_fg))
        if (Build.VERSION.SDK_INT >= 23) { breakStrategy = Layout.BREAK_STRATEGY_HIGH_QUALITY; hyphenationFrequency = Layout.HYPHENATION_FREQUENCY_NONE }
    }

    private fun row() = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; setPadding(0, dp(8), 0, dp(4)) }

    /** The body or details with a leading "For:" / "Why:" / "Undo:" … label on each line in bold. */
    private fun labelled(text: String): CharSequence {
        val s = SpannableString(text)
        var start = 0
        for (ln in text.split('\n')) {
            val colon = ln.indexOf(':')
            if (colon in 1..LABEL_MAX && ln.substring(0, colon).all { it.isLetter() || it == ' ' } && ln[0].isUpperCase())
                s.setSpan(StyleSpan(Typeface.BOLD), start, start + colon + 1, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            start += ln.length + 1
        }
        return s
    }

    private fun weighted() = LinearLayout.LayoutParams(0, -2, 1f).apply { marginStart = dp(4); marginEnd = dp(4) }

    /** One line per label: it shrinks with large font settings instead of wrapping or cutting. */
    private fun bigButton(label: String, outlined: Boolean = false, onClick: () -> Unit): Button =
        (if (outlined) com.google.android.material.button.MaterialButton(this, null, com.google.android.material.R.attr.materialButtonOutlinedStyle)
         else com.google.android.material.button.MaterialButton(this)).apply {
            text = label; textSize = 19f; isAllCaps = false; maxLines = 1; minHeight = dp(60)
            TextViewCompat.setAutoSizeTextTypeUniformWithConfiguration(this, 12, 19, 1, TypedValue.COMPLEX_UNIT_SP)
            setOnClickListener { onClick() }
        }

    /** A choice's whole text, wrapped over a few lines rather than shrunk or cut: it is what gets sent. */
    private fun choiceButton(label: String, onClick: () -> Unit): Button =
        com.google.android.material.button.MaterialButton(this, null, com.google.android.material.R.attr.materialButtonOutlinedStyle).apply {
            text = label; textSize = 17f; isAllCaps = false; maxLines = 4; minHeight = dp(56)
            gravity = Gravity.START or Gravity.CENTER_VERTICAL
            setOnClickListener { onClick() }
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
        private const val SETTLE_MS = 300L
        private const val CONFIRM_MS = 450L
        private const val LABEL_MAX = 12
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

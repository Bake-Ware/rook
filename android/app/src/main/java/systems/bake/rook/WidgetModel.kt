package systems.bake.rook

/**
 * What the home-screen widgets show (see [RookWidgets]), kept apart from RemoteViews so it is
 * unit-tested: the Talk button's label for a voice state, and the rows of the Waiting widget.
 */
object WidgetModel {
    /** The Talk button: [label] under the mic; [active] lights the mic while a session is live. */
    data class Talk(val label: String, val active: Boolean)

    fun talk(state: String): Talk = when (state) {
        "listening" -> Talk("Listening", true)
        "thinking" -> Talk("Thinking", true)
        // A tap while it speaks interrupts, like the app's Talk button.
        "speaking" -> Talk("Tap to stop", true)
        "reconnecting" -> Talk("Connecting", true)
        else -> Talk("Talk", false)
    }

    /** One waiting item: the card it opens, its line, and whether to draw it in the accent colour. */
    data class Row(val id: String, val text: String, val loud: Boolean)

    /** Up to the rows that fit, and how many are waiting in all. */
    data class Waiting(val rows: List<Row>, val total: Int) {
        val more get() = total - rows.size
    }

    /**
     * From the card screen's stack (answered cards are already out of it): what waits on the person.
     * A local alert is only a heads-up about another app's notification, so it is left out. Urgent
     * and failed ones first, then oldest first.
     */
    fun waiting(stack: List<AttentionCard>, max: Int): Waiting {
        val items = stack.filter { !it.local }
            .sortedWith(compareBy<AttentionCard>({ !(it.urgent || it.state == "failed") }, { it.created }))
        return Waiting(items.take(max.coerceAtLeast(0)).map { Row(it.id, rowText(it), it.urgent || it.state == "failed") }, items.size)
    }

    fun rowText(c: AttentionCard): String {
        val line = c.line.trim()
        if (c.state == "failed") return "Didn’t go through: $line"
        val kind = when (c.kind) {
            "ask" -> "Question"
            "hold" -> "Approval"
            "job" -> "Job"
            "alert" -> "Alert"
            else -> ""
        }
        return if (kind.isEmpty()) line else "$kind · $line"
    }

    /** Rows that fit a widget [heightDp] tall: a 40 dp header, then about 30 dp a row. */
    fun rowsFor(heightDp: Int): Int = ((heightDp - 40) / 30).coerceIn(1, 6)
}

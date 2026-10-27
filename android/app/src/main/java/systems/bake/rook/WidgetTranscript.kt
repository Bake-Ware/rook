package systems.bake.rook

/**
 * The conversation the Chat widget shows: the app's chat bubbles, rebuilt from [VoiceBus]'s
 * conversation log by the same rules as MainActivity (a heard or typed line is the person's bubble;
 * reply deltas join one agent bubble per turn; an interrupted reply fades; voice.speak lines are the
 * agent's). Only the newest [capacity] bubbles are kept. Fed on the main thread by
 * [RookWidgets.catchUp]; [snapshot] is read from the widget's list factory on a binder thread.
 * Pure Kotlin for JVM tests.
 */
class WidgetTranscript(private val capacity: Int = 30) : VoiceBus.Listener {
    data class Line(val id: Long, val user: Boolean, val text: String, val faded: Boolean = false)

    private class Bubble(val id: Long, val user: Boolean, var text: String, var faded: Boolean = false)

    private val bubbles = ArrayDeque<Bubble>()
    private val assistantTurns = mutableMapOf<Int, Bubble>()
    private var current: Bubble? = null
    private var generation = -1L
    private var nextId = 0L

    /** The last [VoiceBus] sequence number handled: catch-up replays only what is newer. */
    @Volatile var seen = 0L
        private set

    /** The bubbles to draw, oldest first; a reply that has not said anything yet is left out. */
    @Synchronized fun snapshot(): List<Line> = bubbles.filter { it.text.isNotBlank() }.map { Line(it.id, it.user, it.text, it.faded) }

    @Synchronized private fun add(text: String, user: Boolean): Bubble {
        val b = Bubble(++nextId, user, text)
        bubbles.addLast(b)
        while (bubbles.size > capacity) bubbles.removeFirst()
        return b
    }

    @Synchronized private fun append(b: Bubble, text: String) {
        if (b.text.isNotEmpty()) b.text += " "
        b.text += text
    }

    @Synchronized private fun fade(b: Bubble) { b.faded = true }

    private fun syncConnection() {
        if (generation != VoiceBus.eventGeneration) {
            generation = VoiceBus.eventGeneration
            assistantTurns.clear(); current = null
        }
    }

    override fun onDelivered(seq: Long) { seen = seq }
    override fun onState(state: String) {}
    override fun onError(msg: String) {}

    override fun onUserText(text: String) { current = null; add(text, user = true) }
    override fun onTranscript(text: String) { current = null; add(text, user = true) }
    override fun onTranscript(text: String, turn: Int?) {
        syncConnection()
        if (turn == null) { onTranscript(text); return }
        current = null; add(text, user = true)
    }

    override fun onAssistantDelta(text: String) {
        val b = current ?: add("", user = false).also { current = it }
        append(b, text)
    }
    override fun onAssistantDelta(text: String, turn: Int?) {
        syncConnection()
        if (turn == null) { onAssistantDelta(text); return }
        val b = assistantTurns.getOrPut(turn) { add("", user = false) }
        current = b
        append(b, text)
    }

    override fun onAssistantDone() { current = null }
    override fun onSpoken(text: String) { current = null; add(text, user = false) }
    override fun onInterrupt() { current?.let { fade(it) }; current = null }
}

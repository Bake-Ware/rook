package systems.bake.rook

/** Message identity and expansion survive RecyclerView rebinding; turns are connection-scoped. */
class BubbleDecisions<T> {
    private val users = mutableMapOf<TurnKey, T>()
    private val assistants = mutableMapOf<TurnKey, T>()
    private val decisions = mutableMapOf<TurnKey, Decision>()
    private val expanded = mutableSetOf<T>()
    private val pendingExpand = mutableSetOf<TurnKey>()
    fun target(key: TurnKey): T? = assistants[key] ?: users[key]
    fun register(key: TurnKey, message: T, user: Boolean) {
        val old = target(key)
        (if (user) users else assistants)[key] = message
        val new = target(key)
        if (old != new && old != null && expanded.remove(old) && new != null) expanded.add(new)
        if (new != null && pendingExpand.remove(key)) expanded.add(new)
    }
    fun receive(key: TurnKey, decision: Decision) { decisions[key] = decision }
    fun decision(key: TurnKey?, message: T): Decision? = key?.let { if (target(it) == message) decisions[it] else null }
    fun isExpanded(message: T) = message in expanded
    fun toggle(key: TurnKey?, message: T) {
        if (decision(key, message) == null) return
        if (!expanded.remove(message)) expanded.add(message)
    }
    fun expand(key: TurnKey): T? {
        val message = target(key)
        if (message != null) expanded.add(message) else pendingExpand.add(key)
        return message
    }
}

package systems.bake.rook

/** The settings screen's tabs; the selection is remembered by id so reordering never restores the wrong one. */
object SettingsTabs {
    const val PREF = "settings_tab"
    data class Tab(val id: String, val label: String)
    val ALL = listOf(Tab("voice", "Voice"), Tab("band", "Band"), Tab("permissions", "Permissions"), Tab("app", "App"))

    /** Index of a saved tab id; the first tab when missing or unknown. */
    fun indexOf(id: String?): Int = ALL.indexOfFirst { it.id == id }.coerceAtLeast(0)

    /** The id to store for a selected index (clamped to a real tab). */
    fun idAt(index: Int): String = ALL[index.coerceIn(0, ALL.size - 1)].id
}

/** The settings footer: the most recent status lines, newest last. */
class StatusLog(private val keep: Int = 3) {
    private val lines = ArrayDeque<String>()
    fun add(line: String): String {
        line.lines().map { it.trim() }.filter { it.isNotEmpty() }.forEach { lines.addLast(it) }
        while (lines.size > keep) lines.removeFirst()
        return text()
    }
    fun text(): String = lines.joinToString("\n")
}

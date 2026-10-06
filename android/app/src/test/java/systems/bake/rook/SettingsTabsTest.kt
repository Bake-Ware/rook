package systems.bake.rook

import org.junit.Assert.*
import org.junit.Test

class SettingsTabsTest {
    @Test fun restoresByIdAndFallsBackToFirst() {
        assertEquals(listOf("voice", "band", "permissions", "app"), SettingsTabs.ALL.map { it.id })
        assertEquals(2, SettingsTabs.indexOf("permissions"))
        assertEquals(0, SettingsTabs.indexOf(null))
        assertEquals(0, SettingsTabs.indexOf("activity"))
        for (i in SettingsTabs.ALL.indices) assertEquals(i, SettingsTabs.indexOf(SettingsTabs.idAt(i)))
        assertEquals("app", SettingsTabs.idAt(99)); assertEquals("voice", SettingsTabs.idAt(-1))
    }

    @Test fun statusFooterKeepsTheNewestLines() {
        val log = StatusLog(keep = 3)
        assertEquals("", log.text())
        log.add("one"); log.add("two\n\n  three  ")
        assertEquals("one\ntwo\nthree", log.text())
        assertEquals("two\nthree\nfour", log.add("four"))
        log.add("   ")
        assertEquals("two\nthree\nfour", log.text())
    }
}

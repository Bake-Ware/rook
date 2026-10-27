package systems.bake.rook

import org.junit.Assert.*
import org.junit.Test

class ScreenModelTest {
    private fun b(l: Int, t: Int, r: Int, bt: Int) = intArrayOf(l, t, r, bt)

    /** The Rook settings screen from the 2026-10-27 journal, cut down. */
    private fun settings(): SNode = SNode(cls = "FrameLayout", children = listOf(
        SNode(desc = "Back to conversation", id = "systems.bake.rook:id/btnBack", clickable = true, bounds = b(11, 106, 146, 241)),
        SNode(text = "SETTINGS", bounds = b(146, 139, 410, 207)),
        SNode(desc = "Voice", clickable = false, selected = true, children = listOf(SNode(text = "Voice", selected = true))),
        SNode(desc = "Band", clickable = true, bounds = b(270, 252, 540, 365), children = listOf(SNode(text = "Band"))),
        SNode(id = "systems.bake.rook:id/settingsScroll", scrollable = true, children = listOf(
            SNode(text = "CONVERSATION MODE"),
            SNode(text = "Conversation", desc = "Conversation mode", id = "systems.bake.rook:id/voiceMode",
                editable = true, clickable = true, bounds = b(90, 513, 990, 648)),
            SNode(clickable = true, bounds = b(0, 700, 1080, 800), children = listOf(
                SNode(text = "Wi-Fi"), SNode(text = "Connected to home"))),
            SNode(text = "Dark theme", checkable = true, checked = true, clickable = true),
        )),
    ))

    @Test fun summaryListsActionsOnceAndKeepsOtherText() {
        val s = ScreenModel.summary(listOf(settings()))
        val items = s.getJSONArray("items")
        val labels = (0 until items.length()).map { items.getJSONObject(it).getString("label") }
        assertEquals(listOf("Back to conversation", "Band", "settingsScroll", "Conversation mode", "Wi-Fi", "Dark theme"), labels)
        assertEquals("btnBack", items.getJSONObject(0).getString("id"))
        assertEquals("button", items.getJSONObject(0).getString("kind"))
        assertEquals("[78,173]", items.getJSONObject(0).getJSONArray("at").toString())
        val mode = items.getJSONObject(3)
        assertEquals("field", mode.getString("kind"))
        assertEquals("Conversation", mode.getString("value"))
        val wifi = items.getJSONObject(4)
        assertEquals("Connected to home", wifi.getString("detail"))
        assertTrue(items.getJSONObject(5).getBoolean("checked"))
        assertEquals("toggle", items.getJSONObject(5).getString("kind"))
        val text = s.getJSONArray("text")
        val lines = (0 until text.length()).map { text.getString(it) }
        // A row's own texts are not repeated; "Voice" (not clickable) stays text.
        assertEquals(listOf("SETTINGS", "Voice", "Voice", "CONVERSATION MODE"), lines)
        assertTrue(s.getString("rev").length == 12)
    }

    @Test fun summaryCapsItemsAndText() {
        val many = SNode(children = (1..10).map { SNode(text = "row $it", clickable = true) } +
            (1..10).map { SNode(text = "line $it") })
        val s = ScreenModel.summary(listOf(many), maxItems = 3, maxText = 2)
        assertEquals(3, s.getJSONArray("items").length())
        assertEquals(7, s.getInt("more_items"))
        assertEquals(8, s.getInt("more_text"))
    }

    @Test fun passwordsAndHintsAreNeverValues() {
        val root = SNode(children = listOf(
            SNode(text = "••••", editable = true, password = true, hint = "Password", focused = true),
            SNode(text = "Search", editable = true, hint = "Search"),
            SNode(text = "Type here", editable = true, showingHint = true, desc = "Message"),
        ))
        val s = ScreenModel.summary(listOf(root))
        val items = s.getJSONArray("items")
        assertFalse(items.getJSONObject(0).has("value"))
        assertTrue(items.getJSONObject(0).getBoolean("password"))
        assertEquals("Password", items.getJSONObject(0).getString("label"))
        assertEquals("", items.getJSONObject(1).getString("value"))
        assertEquals("", items.getJSONObject(2).getString("value"))
        assertEquals("Message", items.getJSONObject(2).getString("label"))
        assertEquals("Password", s.getString("focus"))
        assertFalse(s.toString().contains("••••"))
    }

    @Test fun revChangesWithWhatIsShownNotWhereItIs() {
        val a = ScreenModel.rev(listOf(SNode(children = listOf(SNode(text = "A", bounds = b(0, 0, 10, 10))))))
        val moved = ScreenModel.rev(listOf(SNode(children = listOf(SNode(text = "A", bounds = b(5, 5, 15, 15))))))
        val other = ScreenModel.rev(listOf(SNode(children = listOf(SNode(text = "B")))))
        val toggled = ScreenModel.rev(listOf(SNode(children = listOf(SNode(text = "A", checkable = true, checked = true)))))
        val hidden = ScreenModel.rev(listOf(SNode(children = listOf(SNode(text = "A", visible = false)))))
        assertEquals(a, moved)
        assertNotEquals(a, other)
        assertNotEquals(a, toggled)
        assertNotEquals(a, hidden)
        assertNotEquals(a, ScreenModel.rev(listOf(SNode(children = listOf(SNode(text = "A"))), SNode())))
    }

    @Test fun popupWindowsAboveTheActiveOneAreRead() {
        val wins = listOf(
            SWindow(type = 3, layer = 30, active = false),                         // status bar (system)
            SWindow(type = ScreenModel.TYPE_APPLICATION, layer = 21, active = false), // the dropdown
            SWindow(type = 2, layer = 25, active = false),                         // keyboard
            SWindow(type = ScreenModel.TYPE_APPLICATION, layer = 20, active = true),  // the activity
            SWindow(type = ScreenModel.TYPE_APPLICATION, layer = 10, active = false), // an app below
        )
        assertEquals(listOf(3, 1), ScreenModel.pickWindows(wins))
        assertEquals(emptyList<Int>(), ScreenModel.pickWindows(wins.map { SWindow(it.type, it.layer, false) }))
        assertEquals(listOf(0), ScreenModel.pickWindows(listOf(SWindow(ScreenModel.TYPE_APPLICATION, 1, true))))
    }

    private fun form() = listOf(SNode(children = listOf(
        SNode(text = "Email", bounds = b(40, 100, 300, 140)),
        SNode(editable = true, id = "app:id/email", bounds = b(40, 150, 1040, 230)),
        SNode(text = "Name", bounds = b(40, 300, 200, 360)),
        SNode(editable = true, id = "app:id/name", bounds = b(220, 300, 1040, 360)),
        SNode(editable = true, hint = "Search", id = "app:id/q", bounds = b(40, 600, 1040, 680)),
        SNode(editable = true, labeledBy = "Phone", password = false, bounds = b(40, 800, 1040, 880)),
    )))

    @Test fun fieldsAreFoundByHintCaptionLabelForAndId() {
        val roots = form()
        assertEquals("app:id/q", ScreenModel.findFields(roots, "search", null, false).single().id)
        assertEquals("app:id/email", ScreenModel.findFields(roots, "Email", null, false).single().id) // caption above
        assertEquals("app:id/name", ScreenModel.findFields(roots, "name", null, false).single().id)   // caption left
        assertEquals("Phone", ScreenModel.findFields(roots, "phone", null, false).single().labeledBy)
        assertEquals("app:id/q", ScreenModel.findFields(roots, null, "q", false).single().id)
        assertEquals("app:id/q", ScreenModel.findFields(roots, "sea", null, true).single().id)
        assertTrue(ScreenModel.findFields(roots, "Address", null, false).isEmpty())
        // No label: the focused one, else the only one, else none.
        assertTrue(ScreenModel.findFields(roots, null, null, false).isEmpty())
        val one = listOf(SNode(children = listOf(SNode(editable = true, id = "x"))))
        assertEquals("x", ScreenModel.findFields(one, null, null, false).single().id)
        val focused = listOf(SNode(children = listOf(SNode(editable = true, id = "a"), SNode(editable = true, id = "b", focused = true))))
        assertEquals("b", ScreenModel.findFields(focused, null, null, false).single().id)
        assertEquals("Search", ScreenModel.fieldName(ScreenModel.findFields(roots, null, "q", false).single()))
    }
}

package systems.bake.rook.core.worker

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The screen model the ui caps read and act through (brief AP5; 0.5's ScreenModel rules), on screens
 * made here: what ui.screen gives, which node a click lands on, which field ui.type picks, the PIN
 * pad phone.unlock presses, and that nothing on screen is cut (0.5 cut labels, lines and lists).
 */
class ScreenTest {
    private fun at(l: Int, t: Int, r: Int, b: Int) = intArrayOf(l, t, r, b)

    private fun settings(): List<SNode> = Screen.link(listOf(
        SNode(cls = "FrameLayout", bounds = at(0, 0, 1080, 2400), children = listOf(
            SNode(text = "Settings", bounds = at(40, 100, 400, 180)),
            SNode(editable = true, hint = "Search settings", id = "com.android.settings:id/search", bounds = at(40, 200, 1040, 300)),
            SNode(scrollable = true, id = "com.android.settings:id/list", bounds = at(0, 320, 1080, 2400), children = listOf(
                SNode(clickable = true, bounds = at(0, 320, 1080, 480), children = listOf(
                    SNode(text = "Network & internet", bounds = at(160, 340, 900, 400)),
                    SNode(text = "Mobile, Wi-Fi, hotspot", bounds = at(160, 410, 900, 460)))),
                SNode(clickable = true, bounds = at(0, 480, 1080, 640), children = listOf(
                    SNode(text = "Connected devices", bounds = at(160, 500, 900, 560)))),
                SNode(checkable = true, checked = true, clickable = true, text = "Dark theme", bounds = at(0, 640, 1080, 800)))),
        ))))

    @Test fun theScreenNamesItemsWithTheirCentresAndTheOtherText() {
        val shot = Screen.summary(settings(), pkg = "com.android.settings")
        assertEquals("com.android.settings", shot.getString("package"))
        val items = shot.getJSONArray("items")
        val row = (0 until items.length()).map { items.getJSONObject(it) }.first { it.getString("text") == "Network & internet" }
        assertEquals("button", row.getString("kind"))
        assertEquals("Mobile, Wi-Fi, hotspot", row.getString("detail"))
        assertEquals(540, row.getInt("x"))
        assertEquals(400, row.getInt("y"))
        val field = (0 until items.length()).map { items.getJSONObject(it) }.first { it.getString("kind") == "field" }
        assertEquals("Search settings", field.getString("text"))
        assertEquals("search", field.getString("id"))
        val toggle = (0 until items.length()).map { items.getJSONObject(it) }.first { it.getString("kind") == "toggle" }
        assertTrue(toggle.getBoolean("checked"))
        assertEquals("Settings", shot.getJSONArray("text").getString(0))
        assertFalse(shot.has("more_items"))
    }

    @Test fun nothingIsCutUnlessTheCallerAsks() {
        val long = "word ".repeat(400).trim()                       // 1999 characters: 0.5 cut labels at 80, lines at 160
        val rows = (1..500).map { SNode(clickable = true, text = "Row $it", bounds = at(0, it * 10, 100, it * 10 + 10)) }
        val roots = Screen.link(listOf(SNode(children = rows + SNode(text = long) + SNode(clickable = true, text = long))))
        val shot = Screen.summary(roots)
        assertEquals(501, shot.getJSONArray("items").length())      // 0.5: 60 unless asked, never more than 300
        assertEquals(long, shot.getJSONArray("text").getString(0))
        assertEquals(long, shot.getJSONArray("items").getJSONObject(500).getString("text"))
        val asked = Screen.summary(roots, maxItems = 10, maxText = 0)
        assertEquals(10, asked.getJSONArray("items").length())
        assertEquals(491, asked.getInt("more_items"))
        assertEquals(1, asked.getInt("more_text"))
    }

    @Test fun aDeepTreeIsReadToTheBottom() {
        var node = SNode(clickable = true, text = "the bottom")
        repeat(200) { node = SNode(children = listOf(node)) }           // 0.5 stopped at depth 60
        val roots = Screen.link(listOf(node))
        assertEquals("the bottom", Screen.summary(roots).getJSONArray("items").getJSONObject(0).getString("text"))
        assertEquals(1, Screen.find(roots, "the bottom", null, null, false).size)
    }

    @Test fun aClickLandsOnTheNearestClickableParent() {
        val roots = settings()
        val hit = Screen.find(roots, "connected devices", null, null, false).single()
        val target = Screen.clickTarget(hit, false)!!
        assertTrue(target.clickable)
        assertSame(hit.parent, target)
        assertEquals(1, Screen.find(roots, "network", null, null, true).size)
        assertEquals(0, Screen.find(roots, "network", null, null, false).size)
        assertEquals(1, Screen.find(roots, null, null, "list", false).size)          // an id without its package
        assertTrue(Screen.near(roots, "networks", null, null).toString().contains("Network & internet"))
    }

    @Test fun aScrollTakesTheListHoldingTheMatchElseTheOutermost() {
        val roots = settings()
        val list = Screen.scrollTarget(roots, null)!!
        assertEquals("com.android.settings:id/list", list.id)
        assertSame(list, Screen.scrollTarget(roots, Screen.find(roots, "Dark theme", null, null, false).single()))
    }

    @Test fun typingFindsAFieldByItsHintItsCaptionOrFocus() {
        val roots = settings()
        assertEquals(1, Screen.findFields(roots, "search settings", null, false).size)
        assertEquals(1, Screen.findFields(roots, null, null, false).size)            // the only field
        val form = Screen.link(listOf(SNode(children = listOf(
            SNode(text = "Name", bounds = at(0, 0, 200, 50)),
            SNode(editable = true, bounds = at(0, 60, 600, 120)),
            SNode(text = "Email", bounds = at(0, 200, 200, 250)),
            SNode(editable = true, focused = true, text = "a@b.c", bounds = at(0, 260, 600, 320)),
        ))))
        val email = Screen.findFields(form, "email", null, false).single()
        assertEquals("a@b.c", email.value())
        assertSame(email, Screen.findFields(form, null, null, false).single())       // the focused one
        val (pick, why) = Screen.chooseField(Screen.fields(form), -1)
        assertNull(pick)
        assertEquals("2 fields match; pass index (0 = top)", why)
        assertEquals("a@b.cd", Screen.newText(email, "d", true))
    }

    @Test fun aPasswordIsNeverReadNorAppendedTo() {
        val roots = Screen.link(listOf(SNode(editable = true, password = true, text = "••••", hint = "Password")))
        val item = Screen.summary(roots).getJSONArray("items").getJSONObject(0)
        assertFalse(item.has("value"))
        assertTrue(item.getBoolean("password"))
        assertNull(Screen.newText(roots[0], "x", true))
    }

    @Test fun theRevisionChangesWithWhatIsShownNotWhereItIs() {
        val a = Screen.rev(Screen.link(listOf(SNode(text = "One", bounds = at(0, 0, 10, 10)))))
        assertEquals(a, Screen.rev(Screen.link(listOf(SNode(text = "One", bounds = at(5, 5, 15, 15))))))
        assertNotEquals(a, Screen.rev(Screen.link(listOf(SNode(text = "Two")))))
    }

    @Test fun thePopupAboveTheAppIsReadFirstAndOtherAppsAreLeftOut() {
        val wins = listOf(SWindow(1, 1, true, "app"), SWindow(1, 3, false, "app"), SWindow(1, 2, false, "other"), SWindow(2, 4, false, "ime"))
        assertEquals(listOf(1, 0), Screen.pickWindows(wins))
    }

    @Test fun theLockScreensPinPadIsFoundByItsKeys() {
        val keys = (0..9).map { d ->
            SNode(clickable = true, id = "com.android.systemui:id/key$d", children = listOf(SNode(text = "$d")))
        }
        val roots = Screen.link(listOf(SNode(children = keys + SNode(clickable = true, id = "com.android.systemui:id/key_enter", desc = "Enter"))))
        val pad = Screen.pinKeys(roots)
        assertEquals(10, pad.size)
        assertEquals("com.android.systemui:id/key7", pad.getValue('7').id)
        assertEquals("com.android.systemui:id/key_enter", Screen.enterKey(roots)!!.id)
        // A pad without view ids: the digits as clickables' text.
        val plain = Screen.link(listOf(SNode(children = (0..9).map { SNode(clickable = true, text = "$it") } + SNode(clickable = true, text = "OK"))))
        assertEquals(10, Screen.pinKeys(plain).size)
        assertEquals("OK", Screen.enterKey(plain)!!.text)
        assertTrue(Screen.pinKeys(settings()).isEmpty())
    }

    @Test fun aLockScreensNotificationsAreNoPinPad() {
        // After a reboot (brief AP8): the lock screen before its pad comes up, a notification group showing its count
        // ("3", clickable through its expand button) and a timer's notification. No digit key is there.
        val shade = Screen.link(listOf(SNode(id = "com.android.systemui:id/notification_stack_scroller", children = listOf(
            SNode(clickable = true, id = "com.android.systemui:id/expand_button", children = listOf(SNode(text = "3"))),
            SNode(clickable = true, id = "com.android.systemui:id/expandableNotificationRow", children = listOf(
                SNode(text = "Timer"), SNode(text = "\u221203:38"))),
            SNode(clickable = true, id = "com.android.systemui:id/device_entry_icon_view", desc = "Device locked")))))
        assertTrue(Screen.pinKeys(shade).isEmpty())
        // The pad then comes up beside them: found by its ids, the count is not taken for a key.
        val keys = (0..9).map { d -> SNode(clickable = true, id = "com.android.systemui:id/key$d", children = listOf(SNode(text = "$d"))) }
        val withPad = Screen.link(listOf(SNode(children = listOf(
            SNode(clickable = true, id = "com.android.systemui:id/expand_button", children = listOf(SNode(text = "3")))) + keys)))
        assertEquals("com.android.systemui:id/key3", Screen.pinKeys(withPad).getValue('3').id)
    }

    @Test fun theLockScreensIconAndThePinFieldsLength() {
        val lock = Screen.link(listOf(SNode(children = listOf(
            SNode(text = "08 47"), SNode(id = "com.android.systemui:id/device_entry_icon_fg", desc = "Device locked")))))
        assertEquals("Device locked", Screen.unlockIcon(lock)!!.desc)
        assertNull(Screen.unlockIcon(settings()))
        val pad = Screen.link(listOf(SNode(children = listOf(
            SNode(editable = true, password = true, id = "com.android.systemui:id/pinEntry", text = "\u2022\u2022\u2022", desc = "PIN area")))))
        assertEquals(3, Screen.pinLength(pad))                 // the dots Android shows, never the digits
        assertEquals(0, Screen.pinLength(Screen.link(listOf(SNode(editable = true, password = true, id = "x:id/pinEntry")))))
        assertEquals(-1, Screen.pinLength(Screen.link(listOf(SNode(text = "no field")))))
    }
}

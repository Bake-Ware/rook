package systems.bake.rook

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.io.File

class AttentionRulesTest {
    private val defaults = JSONObject(listOf(File("src/main/assets/attention_rules.json"), File("app/src/main/assets/attention_rules.json"))
        .first { it.exists() }.readText())
    private val rules = AttentionRules.effective(defaults, null)
    private fun item(pkg: String, app: String, title: String, text: String = "") = AttentionRules.Item(pkg, app, title, text)
    private fun pri(i: AttentionRules.Item, starred: List<String> = emptyList()) = rules.classify(i, starred).priority

    @Test fun phrasesMatchAtWordStarts() {
        assertTrue(AttentionRules.has("Please evacuate now", "evacuat"))
        assertFalse(AttentionRules.has("Ceasefire talks", "fire"))
        assertTrue(AttentionRules.has("50% off today", "% off"))
        assertTrue(AttentionRules.has("Call 911", "911"))
        assertTrue(AttentionRules.has("Mom (2 messages)", "mom", wholeWord = true))
        assertFalse(AttentionRules.has("Momentum", "mom", wholeWord = true))
        assertFalse(AttentionRules.has("anything", "  "))
    }

    @Test fun urgentWordsFromPeopleAndSchoolRing() {
        assertEquals("urgent", pri(item("com.google.android.apps.messaging", "Messages", "Alex", "There's a fire at the house, call me")))
        assertEquals("urgent", pri(item("com.classdojo.android", "ClassDojo", "Lincoln Elementary", "Lockdown in effect")))
        val v = rules.classify(item("com.whatsapp", "WhatsApp", "Sam", "I'm hurt, pick up now"))
        assertEquals("urgent", v.category)
    }

    @Test fun urgentWordsElsewhereDoNotRing() {
        // News, shops and promos say "fire" and "police" all day: never an alarm at night.
        assertEquals("ignore", pri(item("com.cnn.mobile", "CNN", "Police respond to fire downtown")))
        assertEquals("ignore", pri(item("com.amazon.mshop.android.shopping", "Amazon Shopping", "Fire TV Stick deal")))
        assertEquals("normal", pri(item("com.some.game", "Some Game", "Emergency event live now")))
        assertEquals("ignore", pri(item("com.google.android.apps.messaging", "Messages", "Store", "Fire sale! 50% off")))
        assertEquals("ignore", pri(item("com.google.android.deskclock", "Clock", "Alarm", "7:00 AM")))
    }

    @Test fun familySchoolHealthAreFamily() {
        assertEquals("family", pri(item("com.google.android.apps.messaging", "Messages", "Jane Doe", "Dinner at 6?"), listOf("Jane Doe")))
        assertEquals("normal", pri(item("com.google.android.apps.messaging", "Messages", "Jane Doe", "Dinner at 6?")))
        assertEquals("school", rules.classify(item("com.remind101", "Remind", "Ms. Smith", "Field trip form due")).category)
        assertEquals("school", rules.classify(item("com.google.android.gm", "Gmail", "District", "School closes early Friday")).category)
        assertEquals("health", rules.classify(item("epic.mychart.android", "MyChart", "New test result")).category)
        assertEquals("ignore", pri(item("com.cvs.launchers.cvs", "CVS", "20% off vitamins", "Rewards week")))
    }

    @Test fun workAccountsAndSchoolBoardsAreNotSchool() {
        // Seen on bakephone 2026-10-27: a work sign-in prompt and an employer's name sorted as school.
        assertEquals("normal", pri(item("com.azure.authenticator", "Authenticator", "New sign-in request for your work or school account", "")))
        assertEquals("normal", pri(item("com.microsoft.office.outlook", "Outlook", "Mary Lemons", "Texas Association of School Boards: agenda")))
        assertEquals("school", rules.classify(item("com.google.android.calendar", "Calendar", "Advising appt school", "11:30 AM")).category)
    }

    @Test fun aChoiceGoesInFrontAndReplacesTheLastOneForThatApp() {
        val a = AttentionRules.withChoice(null, "Google", "com.google.android.googlequicksearchbox", "ignore")
        assertTrue(a.getBoolean("extend_default"))
        val google = item("com.google.android.googlequicksearchbox", "Google", "75° in San Antonio", "Mostly sunny")
        assertEquals("ignore", AttentionRules.effective(defaults, a).classify(google).priority)
        assertEquals("ignore", AttentionRules.choiceOf(a, "Google", "com.google.android.googlequicksearchbox"))
        // The same app again: one rule, the new choice; by name only for an app without a package.
        val b = AttentionRules.withChoice(AttentionRules.withChoice(a, "Google", "com.google.android.googlequicksearchbox", "family"),
            "Chat", "", "urgent")
        assertEquals(2, b.getJSONArray("rules").length())
        assertEquals("family", AttentionRules.effective(defaults, b).classify(google).priority)
        assertEquals(AttentionRules.Verdict("urgent", "urgent", "yours: ring me (Chat)"),
            AttentionRules.effective(defaults, b).classify(item("com.google.android.apps.dynamite", "Chat", "Autumn", "ok")))
        // Back to the phone's own rules; rules that aren't the person's are kept.
        val hub = JSONObject("""{"extend_default":true,"family_names":["Autumn"],"rules":[{"priority":"family","match":{"words":["x"]},"note":"hub"}]}""")
        val c = AttentionRules.withChoice(AttentionRules.withChoice(hub, "Chat", "", "urgent"), "Chat", "", "default")
        assertEquals(1, c.getJSONArray("rules").length())
        assertEquals("hub", c.getJSONArray("rules").getJSONObject(0).getString("note"))
        assertEquals("Autumn", c.getJSONArray("family_names").getString(0))
        assertNull(AttentionRules.choiceOf(c, "Chat", ""))
        // Every choice is a rule the phone (and the hub's check) accepts.
        val all = AttentionRules.CHOICES.keys.fold(null as JSONObject?) { d, p -> AttentionRules.withChoice(d, "App$p", "com.app.$p", p) }!!
        assertEquals(4, AttentionRules.rulesOf(all.getJSONArray("rules")).size)
    }

    @Test fun shippingPromoNewsletterIgnored() {
        assertEquals("ignore", pri(item("com.google.android.gm", "Gmail", "Your package", "Out for delivery today")))
        assertEquals("ignore", pri(item("com.google.android.gm", "Gmail", "Weekend coupon", "Use code SAVE")))
        assertEquals("ignore", pri(item("com.google.android.gm", "Gmail", "The weekly digest", "")))
    }

    @Test fun overrideReplacesOrExtends() {
        val trust = JSONObject("""{"rules":[{"priority":"urgent","category":"trusted","match":{"senders":["Coach Dana"]},"note":"trust map"}],
            "family_names":["Grandma"],"extend_default":true}""")
        val r = AttentionRules.effective(defaults, trust)
        val v = r.classify(item("com.google.android.apps.messaging", "Messages", "Coach Dana", "Practice moved"))
        assertEquals(AttentionRules.Verdict("urgent", "trusted", "trust map"), v)
        assertEquals("family", r.classify(item("org.thoughtcrime.securesms", "Signal", "Grandma", "Hi")).priority)
        assertEquals(rules.rules.size + 1, r.rules.size)
        // Without extend_default the override is all there is.
        val only = AttentionRules.effective(defaults, JSONObject("""{"rules":[{"priority":"ignore","match":{"words":["x"]}}],"default":"ignore"}"""))
        assertEquals(1, only.rules.size)
        assertEquals("ignore", only.classify(item("p", "App", "anything")).priority)
    }

    @Test fun invalidRulesAreSkipped() {
        val r = AttentionRules.parse(JSONObject("""{"rules":[{"priority":"loud","match":{"words":["x"]}},{"priority":"family","match":{}},
            {"priority":"family","match":{"words":["x"]}}]}"""))
        assertEquals(1, r.rules.size)
        assertEquals("family", r.rules[0].category)
    }

    @Test fun summaryIsShortAndOnlyAFewWords() {
        val s = AttentionRules.summary("ClassDojo", "school", "Lincoln Elementary — Ms. Smith", "Pickup moved to 2pm today because of the weather alert in the area")
        assertTrue(s, s.startsWith("ClassDojo: school — Lincoln Elementary"))
        assertTrue(s, s.endsWith("Pickup moved to 2pm today…"))
        val long = AttentionRules.summary("A".repeat(80), "family", "T".repeat(200), "x ".repeat(100))
        assertTrue(long.length <= AttentionRules.SUMMARY_MAX)
        assertEquals("Messages: family — Mom", AttentionRules.summary("Messages", "family", "Mom", ""))
    }
}

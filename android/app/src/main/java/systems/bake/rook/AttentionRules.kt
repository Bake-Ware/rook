package systems.bake.rook

import org.json.JSONArray
import org.json.JSONObject

/**
 * Local notification classifier: urgent | family | normal | ignore.
 *
 * Rules are evaluated in order; the first that matches wins, else [default].
 * A rule matches when ANY list in `match` hits, `scope` (if given) also hits,
 * and `except` (if given) does not. Lists:
 *  - packages: substrings of the posting app's package name
 *  - app_labels: phrases in the app's label
 *  - words: phrases in title + text (case-insensitive, at a word start, so "evacuat" finds "evacuate")
 *  - senders: names in the title (whole words); "@family" expands to family_names + starred contacts
 * Format (assets/attention_rules.json, or an override pushed with attention.rules_set):
 *   {"family_names":[...], "family_starred":true, "rules":[{priority, category, match, scope?, except?, note?}]}
 * Nothing here leaves the phone except [summary] for urgent/family items.
 */
class AttentionRules(val rules: List<Rule>, val familyNames: List<String> = emptyList(),
                     val familyStarred: Boolean = true, val default: String = NORMAL) {

    class Match(val packages: List<String> = emptyList(), val appLabels: List<String> = emptyList(),
                val words: List<String> = emptyList(), val senders: List<String> = emptyList()) {
        val empty get() = packages.isEmpty() && appLabels.isEmpty() && words.isEmpty() && senders.isEmpty()
        fun hits(item: Item, family: Collection<String>): Boolean {
            val pkg = item.pkg.lowercase()
            if (packages.any { it.isNotBlank() && pkg.contains(it.lowercase()) }) return true
            if (appLabels.any { has(item.app, it) }) return true
            val body = item.title + "\n" + item.text
            if (words.any { has(body, it) }) return true
            val names = senders.flatMap { if (it == FAMILY_TOKEN) family else listOf(it) }
            return names.any { has(item.title, it, wholeWord = true) }
        }
    }

    class Rule(val priority: String, val category: String, val match: Match, val scope: Match? = null,
               val except: Match? = null, val note: String = "")

    data class Item(val pkg: String, val app: String, val title: String, val text: String)
    data class Verdict(val priority: String, val category: String, val note: String = "")

    /** First matching rule's verdict. [starred] are starred-contact names (only used when family_starred). */
    fun classify(item: Item, starred: Collection<String> = emptyList()): Verdict {
        val family = familyNames + if (familyStarred) starred else emptyList()
        for (r in rules) {
            if (r.match.empty || !r.match.hits(item, family)) continue
            if (r.scope != null && !r.scope.empty && !r.scope.hits(item, family)) continue
            if (r.except != null && !r.except.empty && r.except.hits(item, family)) continue
            return Verdict(r.priority, r.category, r.note)
        }
        return Verdict(default, "other")
    }

    companion object {
        const val URGENT = "urgent"; const val FAMILY = "family"; const val NORMAL = "normal"; const val IGNORE = "ignore"
        val PRIORITIES = setOf(URGENT, FAMILY, NORMAL, IGNORE)
        const val FAMILY_TOKEN = "@family"
        const val SUMMARY_MAX = 140

        /** Phrase at a word start (or anywhere when the phrase starts with a symbol, like "% off"). */
        fun has(haystack: String, phrase: String, wholeWord: Boolean = false): Boolean {
            val p = phrase.trim().lowercase()
            if (p.isEmpty()) return false
            val h = haystack.lowercase()
            var i = h.indexOf(p)
            while (i >= 0) {
                val startOk = !p.first().isLetterOrDigit() || i == 0 || !h[i - 1].isLetterOrDigit()
                val end = i + p.length
                val endOk = !wholeWord || !p.last().isLetterOrDigit() || end >= h.length || !h[end].isLetterOrDigit()
                if (startOk && endOk) return true
                i = h.indexOf(p, i + 1)
            }
            return false
        }

        private fun strings(a: JSONArray?): List<String> =
            (0 until (a?.length() ?: 0)).mapNotNull { a?.opt(it) as? String }.map { it.trim() }.filter { it.isNotEmpty() }

        private fun match(o: JSONObject?): Match? = o?.let {
            Match(strings(it.optJSONArray("packages")), strings(it.optJSONArray("app_labels")),
                strings(it.optJSONArray("words")), strings(it.optJSONArray("senders")))
        }

        /** Rules from JSON; entries with an unknown priority or no match are skipped. */
        fun rulesOf(a: JSONArray?): List<Rule> = (0 until (a?.length() ?: 0)).mapNotNull { i ->
            val o = a?.optJSONObject(i) ?: return@mapNotNull null
            val priority = o.optString("priority").lowercase()
            val m = match(o.optJSONObject("match"))
            if (priority !in PRIORITIES || m == null || m.empty) null
            else Rule(priority, o.optString("category").ifBlank { priority }, m, match(o.optJSONObject("scope")),
                match(o.optJSONObject("except")), o.optString("note"))
        }

        fun parse(doc: JSONObject): AttentionRules = AttentionRules(rulesOf(doc.optJSONArray("rules")),
            strings(doc.optJSONArray("family_names")), doc.optBoolean("family_starred", true),
            doc.optString("default").lowercase().takeIf { it in PRIORITIES } ?: NORMAL)

        /** The effective rules: an override replaces the defaults, or with extend_default=true goes in front of them. */
        fun effective(defaults: JSONObject, override: JSONObject?): AttentionRules {
            val base = parse(defaults)
            if (override == null) return base
            val o = parse(override)
            if (!override.optBoolean("extend_default", false)) return o
            return AttentionRules(o.rules + base.rules, (o.familyNames + base.familyNames).distinct(),
                if (override.has("family_starred")) o.familyStarred else base.familyStarred,
                if (override.has("default")) o.default else base.default)
        }

        /** What the person may choose for an app (Sort on the Attention tab or the hub's Feed page). */
        val CHOICES = linkedMapOf(URGENT to "Ring me", FAMILY to "Tell me", NORMAL to "Just list it", IGNORE to "Hide it")
        private val CHOICE_CATEGORY = mapOf(URGENT to "urgent", FAMILY to "important", NORMAL to "other", IGNORE to "hidden")
        /** The note that marks the person's own choices (the hub writes the same). */
        const val YOURS = "yours:"

        private fun isChoiceFor(rule: JSONObject, app: String, pkg: String): Boolean {
            if (!rule.optString("note").startsWith(YOURS)) return false
            val m = rule.optJSONObject("match") ?: return false
            val pkgs = strings(m.optJSONArray("packages")).map { it.lowercase() }
            val labels = strings(m.optJSONArray("app_labels")).map { it.lowercase() }
            return (pkg.isNotBlank() && pkgs == listOf(pkg.trim().lowercase())) || (labels == listOf(app.trim().lowercase()))
        }

        /**
         * The rules override with the person's choice for one app in front, replacing an earlier choice for it;
         * a [priority] that isn't a choice ("default") just removes it. Matches by package when known, else by
         * the app's name. Other rules, and the rest of the document, are kept.
         */
        fun withChoice(override: JSONObject?, app: String, pkg: String, priority: String): JSONObject {
            val doc = override?.let { JSONObject(it.toString()) } ?: JSONObject().put("extend_default", true)
            val old = doc.optJSONArray("rules") ?: JSONArray()
            val out = JSONArray()
            if (priority in CHOICES) {
                val match = if (pkg.isNotBlank()) JSONObject().put("packages", JSONArray().put(pkg.trim()))
                    else JSONObject().put("app_labels", JSONArray().put(app.trim()))
                out.put(JSONObject().put("priority", priority).put("category", CHOICE_CATEGORY.getValue(priority))
                    .put("note", "$YOURS ${CHOICES.getValue(priority).lowercase()} (${app.trim()})").put("match", match))
            }
            for (i in 0 until old.length()) {
                val r = old.optJSONObject(i) ?: continue
                if (!isChoiceFor(r, app, pkg)) out.put(r)
            }
            return doc.put("rules", out)
        }

        /** The person's current choice for this app in [override], if any. */
        fun choiceOf(override: JSONObject?, app: String, pkg: String): String? {
            val a = override?.optJSONArray("rules") ?: return null
            for (i in 0 until a.length()) {
                val r = a.optJSONObject(i) ?: continue
                if (isChoiceFor(r, app, pkg)) return r.optString("priority").takeIf { it in CHOICES }
            }
            return null
        }

        private fun clip(s: String, n: Int) = s.trim().let { if (it.length <= n) it else it.take(n - 1).trimEnd() + "…" }

        /** "<App>: <category> — <title clipped>" plus a few words of text, at most 140 chars. The only content that leaves the phone. */
        fun summary(app: String, category: String, title: String, text: String): String {
            val head = "${clip(app.replace('\n', ' '), 30)}: $category — ${clip(title.replace('\n', ' '), 60)}"
            val words = text.split(Regex("\\s+")).filter { it.isNotEmpty() }
            val few = words.take(5).joinToString(" ") + if (words.size > 5) "…" else ""
            return clip(if (few.isEmpty()) head else "$head · $few", SUMMARY_MAX)
        }
    }
}

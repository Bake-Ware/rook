package systems.bake.rook

/**
 * Voice conversation modes. Defaults mirror services/voice/modes.py (a server
 * test checks they match). A blank stored prompt means "use the server default",
 * so the server's wording can improve without overwriting user edits.
 */
object VoiceModes {
    const val DEFAULT = "assistant"
    const val MAX_PROMPT = 2000
    const val PREF_MODE = "voice_mode"
    data class Mode(val id: String, val label: String, val defaultPrompt: String)

    val ALL = listOf(
        Mode("assistant", "Assistant", ""),
        Mode("conversation", "Conversation", "Be a warm, friendly conversation partner. Keep every reply short: one or two simple sentences, then usually ask a question back so the talk keeps going. Use plain, everyday words a child understands. Be kind, patient and encouraging. Keep topics safe and age-appropriate; if something is unsafe or upsetting, gently suggest talking to a grown-up they trust."),
        Mode("dictate", "Dictate", ""),
        Mode("brainstorm", "Brainstorm", "Be an energetic brainstorming partner. Offer a few fresh, varied ideas at a time, build on what the user says, combine and twist ideas, and ask one probing question that pushes the thinking further. Keep it spoken and brief: no lists longer than three items, no long explanations unless asked."),
        Mode("roleplay", "Roleplay", "Play the character or scenario the user describes and stay in character. If no scenario has been given yet, ask what they would like to play. Keep replies short and spoken, move the scene along, and step out of character only if the user asks or something becomes unsafe."),
        Mode("listen", "Active listening", "Mostly listen. Reply with short, warm acknowledgements and occasionally reflect back what you heard or the feeling behind it, in a sentence. Do not give advice, solve problems or change the subject unless the user directly asks for that. A gentle open question is fine when they pause."),
    )

    fun byId(id: String?): Mode = ALL.firstOrNull { it.id == id } ?: ALL.first()
    fun promptKey(id: String) = "voice_mode_prompt_$id"

    /** What to store for an edited prompt: trimmed, capped, blank when it equals the default. */
    fun storedPrompt(mode: Mode, input: String): String {
        val text = input.trim().take(MAX_PROMPT).trim()
        return if (text == mode.defaultPrompt) "" else text
    }

    /** Text shown in the editor: the user's prompt, or the default when none is stored. */
    fun shownPrompt(mode: Mode, stored: String?): String = stored?.takeIf { it.isNotBlank() } ?: mode.defaultPrompt

    /**
     * Input to the conversation-id hash. Assistant keeps the historical scope so
     * existing users keep their history; each other mode gets its own conversation,
     * so a kids' chat or a roleplay never mixes into assistant history.
     */
    fun conversationScope(url: String, token: String, mode: String): String =
        url + "\u0000" + token + (if (byId(mode).id == DEFAULT) "" else "\u0000mode:" + byId(mode).id)
}

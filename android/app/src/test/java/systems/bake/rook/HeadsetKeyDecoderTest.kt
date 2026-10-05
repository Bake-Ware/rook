package systems.bake.rook

import org.junit.Assert.*
import org.junit.Test
import systems.bake.rook.HeadsetKeyDecoder.Action
import systems.bake.rook.HeadsetKeyDecoder.Companion.KEY_ASSIST
import systems.bake.rook.HeadsetKeyDecoder.Companion.KEY_HEADSETHOOK
import systems.bake.rook.HeadsetKeyDecoder.Companion.KEY_MEDIA_PLAY_PAUSE
import systems.bake.rook.HeadsetKeyDecoder.Companion.KEY_SEARCH
import systems.bake.rook.HeadsetKeyDecoder.Companion.KEY_VOICE_ASSIST

class HeadsetKeyDecoderTest {
    @Test fun assistKeysTalkOnceOrInterrupt() {
        val d = HeadsetKeyDecoder()
        for (key in listOf(KEY_VOICE_ASSIST, KEY_ASSIST, KEY_SEARCH)) {
            assertEquals(Action.TALK, d.decide(key, down = true, repeatCount = 0, longPress = false, heldMs = 0, speaking = false))
            assertEquals(Action.CONSUME, d.decide(key, down = true, repeatCount = 3, longPress = true, heldMs = 900, speaking = false))
            assertEquals(Action.CONSUME, d.decide(key, down = false, repeatCount = 0, longPress = false, heldMs = 50, speaking = false))
            assertEquals(Action.INTERRUPT, d.decide(key, down = true, repeatCount = 0, longPress = false, heldMs = 0, speaking = true))
        }
    }

    @Test fun longPressHookTalksExactlyOnce() {
        val d = HeadsetKeyDecoder(longPressMs = 600)
        assertEquals(Action.CONSUME, d.decide(KEY_HEADSETHOOK, true, 0, false, 0, false))
        assertEquals(Action.TALK, d.decide(KEY_HEADSETHOOK, true, 1, true, 500, false))
        assertEquals(Action.CONSUME, d.decide(KEY_HEADSETHOOK, true, 2, true, 550, false))
        assertEquals(Action.CONSUME, d.decide(KEY_HEADSETHOOK, false, 0, false, 900, false))
    }

    @Test fun heldPlayPauseWithoutRepeatsIsALongPressOnRelease() {
        val d = HeadsetKeyDecoder(longPressMs = 600)
        assertEquals(Action.CONSUME, d.decide(KEY_MEDIA_PLAY_PAUSE, true, 0, false, 0, false))
        assertEquals(Action.TALK, d.decide(KEY_MEDIA_PLAY_PAUSE, false, 0, false, 800, false))
    }

    @Test fun shortPressPassesClickToMusicUnlessRookIsSpeaking() {
        val d = HeadsetKeyDecoder(longPressMs = 600)
        assertEquals(Action.CONSUME, d.decide(KEY_MEDIA_PLAY_PAUSE, true, 0, false, 0, false))
        assertEquals(Action.CLICK, d.decide(KEY_MEDIA_PLAY_PAUSE, false, 0, false, 120, false))
        assertEquals(Action.CONSUME, d.decide(KEY_HEADSETHOOK, true, 0, false, 0, true))
        assertEquals(Action.INTERRUPT, d.decide(KEY_HEADSETHOOK, false, 0, false, 120, true))
    }

    @Test fun otherMediaKeysPassThrough() {
        val d = HeadsetKeyDecoder()
        assertEquals(Action.PASS, d.decide(87, true, 0, false, 0, false))   // MEDIA_NEXT
        assertEquals(Action.PASS, d.decide(126, false, 0, false, 0, true))  // MEDIA_PLAY
    }
}

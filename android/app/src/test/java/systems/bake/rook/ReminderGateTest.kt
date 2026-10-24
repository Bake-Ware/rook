package systems.bake.rook

import org.junit.Assert.*
import org.junit.Test

class ReminderGateTest {
    private val quiet = ReminderGate.Signals()

    @Test fun nothingGoingOnPasses() {
        assertNull(ReminderGate.precheck(quiet))
        assertNull(ReminderGate.precheck(quiet.copy(voiceState = "standby")))
    }

    @Test fun doNotDisturbComesBeforeEverything() {
        val all = ReminderGate.Signals(dnd = true, audioMode = MicYieldPolicy.MODE_IN_CALL, telephonyCall = true,
            voiceSession = true, othersRecording = true, musicActive = true, speaking = true)
        assertEquals(ReminderGate.DND, ReminderGate.precheck(all))
        assertEquals(ReminderGate.DND, ReminderGate.precheck(quiet.copy(dnd = true)))
        assertEquals(ReminderGate.CALL, ReminderGate.precheck(all.copy(dnd = false)))
    }

    @Test fun callsComeNextAfterDnd() {
        for (mode in listOf(MicYieldPolicy.MODE_IN_CALL, MicYieldPolicy.MODE_RINGTONE, MicYieldPolicy.MODE_IN_COMMUNICATION)) {
            assertEquals(ReminderGate.CALL, ReminderGate.precheck(quiet.copy(audioMode = mode, othersRecording = true, musicActive = true)))
        }
        assertEquals(ReminderGate.CALL, ReminderGate.precheck(quiet.copy(telephonyCall = true, voiceSession = true)))
    }

    @Test fun rooksOwnCommunicationModeIsNotACall() {
        val s = quiet.copy(audioMode = MicYieldPolicy.MODE_IN_COMMUNICATION, rookOwnsCallMode = true)
        assertNull(ReminderGate.precheck(s))
        // A ringing phone is a call even while Rook holds its own mode.
        assertEquals(ReminderGate.CALL, ReminderGate.precheck(s.copy(audioMode = MicYieldPolicy.MODE_RINGTONE)))
    }

    @Test fun theContractsOrder() {
        val all = quiet.copy(voiceSession = true, othersRecording = true, musicActive = true, speaking = true)
        assertEquals(ReminderGate.VOICE_SESSION, ReminderGate.precheck(all))
        assertEquals(ReminderGate.MIC_IN_USE, ReminderGate.precheck(all.copy(voiceSession = false)))
        assertEquals(ReminderGate.MEDIA, ReminderGate.precheck(quiet.copy(musicActive = true)))
        assertEquals(ReminderGate.SPEAKING, ReminderGate.precheck(quiet.copy(speaking = true)))
    }

    @Test fun rooksOwnSpeechIsSpeakingNotMedia() {
        // voice.speak plays on the music stream too.
        assertEquals(ReminderGate.SPEAKING, ReminderGate.precheck(quiet.copy(musicActive = true, speaking = true)))
    }

    @Test fun aVoiceReplyThinkingOrPlayingIsAVoiceSession() {
        assertEquals(ReminderGate.VOICE_SESSION, ReminderGate.precheck(quiet.copy(voiceState = "thinking")))
        assertEquals(ReminderGate.VOICE_SESSION, ReminderGate.precheck(quiet.copy(voiceState = "speaking")))
    }

    @Test fun recordersExcludeRooksOwnAndTheSystemHotword() {
        val standby = ReminderGate.Recorder(11, 7, false)
        val hotword = ReminderGate.Recorder(12, MicYieldPolicy.SOURCE_HOTWORD, false)
        val camera = ReminderGate.Recorder(13, 5, false)
        assertFalse(ReminderGate.recorders(listOf(standby, hotword), setOf(11)).others)
        assertTrue(ReminderGate.recorders(listOf(standby, camera), setOf(11)).others)
        // No known own session: every client counts (session 0 is never "ours").
        assertTrue(ReminderGate.recorders(listOf(ReminderGate.Recorder(0, 6, false)), setOf()).others)
        assertTrue(ReminderGate.recorders(listOf(standby), setOf()).others)
    }

    @Test fun ourOwnSilencedClientIsNoticed() {
        val mine = ReminderGate.Recorder(21, 6, true)
        val r = ReminderGate.recorders(listOf(mine), setOf(21), mine = 21)
        assertTrue(r.mineSilenced); assertFalse(r.others)
        // Standby silenced is MicArbiter's business (it yields), not the check's own silence.
        assertFalse(ReminderGate.recorders(listOf(ReminderGate.Recorder(11, 7, true)), setOf(11, 21), mine = 21).mineSilenced)
    }

    @Test fun afterTheSample() {
        assertNull(ReminderGate.afterSample(QuietCheck.Outcome.QUIET, false))
        assertEquals(ReminderGate.BUSY, ReminderGate.afterSample(QuietCheck.Outcome.BUSY, true))
        assertEquals(ReminderGate.UNCHECKED, ReminderGate.afterSample(QuietCheck.Outcome.SILENCED, false))
        assertEquals(ReminderGate.UNCHECKED, ReminderGate.afterSample(null, false))
        assertNull(ReminderGate.afterSample(QuietCheck.Outcome.SILENCED, true))
        assertNull(ReminderGate.afterSample(null, true))
    }

    @Test fun sampleLimitsAndShortSamples() {
        assertEquals(1000, ReminderGate.clampSample(0))
        assertEquals(8000, ReminderGate.clampSample(60_000))
        assertEquals(3000, ReminderGate.clampSample(3000))
        assertTrue(ReminderGate.shortSample(1500, 3000))
        assertFalse(ReminderGate.shortSample(1800, 3000))
        assertFalse(ReminderGate.shortSample(3000, 3000))
    }
}

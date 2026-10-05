package systems.bake.rook

import org.junit.Assert.*
import org.junit.Test

class AudioRouteKeeperTest {
    private val NORMAL = AudioRouteKeeper.MODE_NORMAL
    private val COMM = MicYieldPolicy.MODE_IN_COMMUNICATION
    private val CALL = MicYieldPolicy.MODE_IN_CALL
    private val RING = MicYieldPolicy.MODE_RINGTONE

    @Test fun plainStopRestoresModeAndForcedSpeaker() {
        val k = AudioRouteKeeper()
        k.begin(NORMAL, speaker = false); k.speakerForced()
        assertEquals(AudioRouteKeeper.Restore(NORMAL, false), k.end(COMM))
    }

    @Test fun yieldingToACallStillUndoesSpeakerButLeavesCallMode() {
        val k = AudioRouteKeeper()
        k.begin(NORMAL, speaker = false); k.speakerForced()
        assertEquals(AudioRouteKeeper.Restore(null, false), k.end(CALL))
        assertEquals(AudioRouteKeeper.Restore(null, false), k.end(RING))
    }

    @Test fun yieldingToAnotherAppWhileModeIsStillRooksRestoresMode() {
        val k = AudioRouteKeeper()
        k.begin(NORMAL, speaker = false); k.speakerForced()
        assertEquals(AudioRouteKeeper.Restore(NORMAL, false), k.end(COMM))
    }

    @Test fun speakerUntouchedWhenRookDidNotForceIt() {
        val k = AudioRouteKeeper()
        k.begin(NORMAL, speaker = true)
        assertEquals(AudioRouteKeeper.Restore(NORMAL, null), k.end(COMM))
    }

    @Test fun inCommunicationIsNeverSavedAsOldMode() {
        assertEquals(NORMAL, AudioRouteKeeper().begin(COMM, speaker = false).mode)
    }

    @Test fun leftoverRookStateIsNotSavedAsOld() {
        val k = AudioRouteKeeper()
        k.begin(NORMAL, speaker = false); k.speakerForced()
        k.end(COMM)   // restore decided but never applied (e.g. it threw)
        // The next capture sees Rook's own leftovers: keep the original pre-Rook values.
        assertEquals(AudioRouteKeeper.Saved(NORMAL, false), k.begin(COMM, speaker = true))
        assertEquals(AudioRouteKeeper.Restore(NORMAL, false), k.end(COMM))
        k.restored()
        assertEquals(AudioRouteKeeper.Saved(CALL, true), k.begin(CALL, speaker = true))
    }

    @Test fun endWithoutBeginDoesNothing() {
        assertEquals(AudioRouteKeeper.Restore(null, null), AudioRouteKeeper().end(COMM))
    }
}

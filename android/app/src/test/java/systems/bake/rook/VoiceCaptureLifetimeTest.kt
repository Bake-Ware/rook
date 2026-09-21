package systems.bake.rook

import org.junit.Assert.*
import org.junit.Test

class VoiceCaptureLifetimeTest {
    @Test fun sessionEndReleasesCaptureWithWakeOff() {
        var releases = 0
        val life = VoiceCaptureLifetime { releases++ }
        life.wakeEnabled = false
        life.standby = true // stale standby must not retain capture
        life.sessionWanted = true
        life.voiceSession = true
        assertTrue(life.needsCapture)
        life.endSession() // shared by END_SESSION, timeout and exhausted retries
        assertFalse(life.needsCapture)
        assertEquals(1, releases)
    }

    @Test fun standbyWithWakeDisabledDoesNotCapture() {
        var releases = 0
        val life = VoiceCaptureLifetime { releases++ }
        life.standby = true
        life.wakeEnabled = false
        life.reconcile()
        assertFalse(life.needsCapture)
        assertEquals(1, releases)
    }

    @Test fun exhaustedRetriesReleaseCapture() {
        var recording = true
        val life = VoiceCaptureLifetime { recording = false }
        life.wakeEnabled = false
        life.standby = true
        life.voiceSession = true
        life.sessionWanted = true
        life.endSession()
        assertFalse(recording)
        assertFalse(life.sessionWanted)
    }

    @Test fun wakeStandbySurvivesSessionEndAndTurningItOffReleases() {
        var recording = true
        val life = VoiceCaptureLifetime { recording = false }
        life.standby = true
        life.voiceSession = true
        life.sessionWanted = true
        life.endSession()
        assertTrue(recording)
        assertTrue(life.needsCapture)
        life.wakeEnabled = false
        life.reconcile()
        assertFalse(recording)
    }

    @Test fun backgroundKeepsActiveVoiceButReleasesIdleAndTextOnlyCapture() {
        var releases = 0
        val life = VoiceCaptureLifetime { releases++ }
        life.wakeEnabled = false
        life.sessionWanted = true
        life.voiceSession = true
        life.reconcile()
        assertEquals(0, releases)
        life.voiceSession = false
        life.reconcile()
        assertEquals(1, releases)
        life.sessionWanted = false
        life.reconcile()
        assertEquals(2, releases)
    }

    @Test fun throwingEffectsAndStopStillReleaseRecorderAndRestoreMode() {
        for (failed in listOf("aec", "echo", "noise", "stop", "release", "speech", "wake", "speaker")) {
            val calls = mutableListOf<String>()
            fun step(name: String): () -> Unit = {
                calls += name
                if (name == failed) throw IllegalStateException(name)
            }
            cleanupVoiceCapture(step("stop"), step("release"),
                listOf(step("aec"), step("echo"), step("noise")),
                listOf(step("speech"), step("wake"), step("speaker"), step("mode")))
            assertEquals(listOf("aec", "echo", "noise", "stop", "release", "speech", "wake", "speaker", "mode"), calls)
        }
    }
}

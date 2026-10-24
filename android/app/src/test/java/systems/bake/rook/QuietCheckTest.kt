package systems.bake.rook

import org.junit.Assert.*
import org.junit.Test
import kotlin.math.PI
import kotlin.math.pow
import kotlin.math.sin
import kotlin.random.Random

class QuietCheckTest {
    private val sr = QuietCheck.SAMPLE_RATE
    private val rnd = Random(7)

    /** Amplitude whose RMS is [dbfs] for white-ish noise (uniform in [-a, a] has rms a/sqrt(3)). */
    private fun noiseAmp(dbfs: Double) = 32768.0 * 10.0.pow(dbfs / 20) * kotlin.math.sqrt(3.0)

    /** [ms] of audio; [level] gives the target dBFS at time t (s); [tone] adds a steady sine instead of noise. */
    private fun audio(ms: Int, level: (Double) -> Double, tone: Double = 0.0): ShortArray {
        val n = sr * ms / 1000
        return ShortArray(n) { i ->
            val t = i.toDouble() / sr
            val db = level(t)
            val v = if (tone > 0) 32768.0 * 10.0.pow(db / 20) * kotlin.math.sqrt(2.0) * sin(2 * PI * tone * t)
                    else (rnd.nextDouble() * 2 - 1) * noiseAmp(db)
            v.coerceIn(-32768.0, 32767.0).toInt().toShort()
        }
    }

    private fun check(vararg parts: ShortArray): QuietCheck.Result {
        val q = QuietCheck()
        parts.forEach { q.feed(it) }
        return q.result()
    }

    private fun mix(a: ShortArray, b: ShortArray) = ShortArray(minOf(a.size, b.size)) {
        (a[it] + b[it]).coerceIn(-32768, 32767).toShort()
    }

    @Test fun aQuietRoomIsQuiet() {
        val r = check(audio(3000, { -70.0 }))
        assertEquals(QuietCheck.Outcome.QUIET, r.outcome)
        assertEquals(100, r.frames)
        assertTrue(r.peakDbfs in -75..-65)
        assertEquals(0.0, r.activeFrac, 0.05)
    }

    @Test fun aLowSteadyHumIsQuiet() {
        val hum = audio(3000, { -48.0 }, tone = 120.0)
        val r = check(mix(hum, audio(3000, { -70.0 })))
        assertEquals(QuietCheck.Outcome.QUIET, r.outcome)
    }

    @Test fun aLouderSteadyHumIsStillQuietButAVeryLoudOneIsNot() {
        assertEquals(QuietCheck.Outcome.QUIET, check(audio(3000, { -35.0 }, tone = 100.0)).outcome)
        assertEquals(QuietCheck.Outcome.BUSY, check(audio(3000, { -20.0 }, tone = 100.0)).outcome)
    }

    @Test fun speechLikeBurstsAreBusy() {
        // Words: 400 ms at about -30 dBFS with a 5 Hz syllable wobble, 200 ms pauses, over a quiet room.
        val speech = audio(3000, { t -> if ((t % 0.6) < 0.4) -30.0 + 6 * sin(2 * PI * 5 * t) else -70.0 })
        val r = check(speech)
        assertEquals(QuietCheck.Outcome.BUSY, r.outcome)
        assertTrue(r.activeFrac > 0.4)
        assertTrue(r.peakDbfs >= -30)
    }

    @Test fun oneShortSentenceIsEnoughToBeBusy() {
        // 2.5 s of quiet, then half a second of talking.
        val r = check(audio(2500, { -68.0 }), audio(500, { t -> -32.0 + 5 * sin(2 * PI * 4 * t) }))
        assertEquals(QuietCheck.Outcome.BUSY, r.outcome)
    }

    @Test fun aSingleClickIsQuiet() {
        val click = audio(20, { -3.0 })
        val r = check(audio(1500, { -68.0 }), click, audio(1480, { -68.0 }))
        assertEquals(QuietCheck.Outcome.QUIET, r.outcome)
        assertTrue(r.peakDbfs > -30)   // it was heard, it just does not count
    }

    @Test fun aDoorOrACoughUnderAQuarterSecondIsQuiet() {
        val r = check(audio(1200, { -66.0 }), audio(180, { -15.0 }), audio(1620, { -66.0 }))
        assertEquals(QuietCheck.Outcome.QUIET, r.outcome)
    }

    @Test fun continuousLoudSoundLikeATvIsBusy() {
        // The floor itself is loud: the relative rule alone would miss it.
        val tv = audio(3000, { t -> -32.0 + 7 * sin(2 * PI * 3 * t) })
        assertEquals(QuietCheck.Outcome.BUSY, check(tv).outcome)
    }

    @Test fun distantMurmurBelowTheAbsoluteFloorIsQuiet() {
        val murmur = audio(3000, { t -> if ((t % 0.6) < 0.4) -58.0 else -72.0 })
        assertEquals(QuietCheck.Outcome.QUIET, check(murmur).outcome)
    }

    @Test fun allZeroBuffersMeanSilencedNotQuiet() {
        val r = check(ShortArray(sr * 3))
        assertEquals(QuietCheck.Outcome.SILENCED, r.outcome)
        assertEquals(QuietCheck.SILENCE_DBFS.toInt(), r.peakDbfs)
        // Nearly all zero (a stray non-zero frame) is still silenced.
        val mostly = ShortArray(sr * 3).also { it[100] = 5 }
        assertEquals(QuietCheck.Outcome.SILENCED, check(mostly).outcome)
    }

    @Test fun aRecorderWarmingUpWithZerosIsNotSilenced() {
        val r = check(ShortArray(sr / 5), audio(2800, { -69.0 }))
        assertEquals(QuietCheck.Outcome.QUIET, r.outcome)
    }

    @Test fun tooLittleAudioCannotBeJudged() {
        assertEquals(QuietCheck.Outcome.SILENCED, check(audio(600, { -70.0 })).outcome)
        assertEquals(QuietCheck.Outcome.SILENCED, QuietCheck().result().outcome)
    }

    @Test fun bytesAndShortsGiveTheSameAnswer() {
        val pcm = audio(3000, { t -> if ((t % 0.6) < 0.4) -30.0 else -70.0 })
        val bytes = ByteArray(pcm.size * 2).also { b ->
            pcm.forEachIndexed { i, s -> b[2 * i] = (s.toInt() and 0xff).toByte(); b[2 * i + 1] = (s.toInt() shr 8).toByte() }
        }
        val a = QuietCheck().apply { feed(pcm) }.result()
        // Odd chunk sizes, like a recorder handing over 20 ms frames.
        val q = QuietCheck()
        var off = 0
        while (off < bytes.size) { val n = minOf(640, bytes.size - off); q.feedBytes(bytes.copyOfRange(off, off + n)); off += n }
        assertEquals(a, q.result())
    }

    @Test fun runsBridgeOneQuietFrameButNotTwo() {
        fun runs(s: String) = QuietCheck.countedRuns(BooleanArray(s.length) { s[it] == 'x' })
        assertEquals(8, runs("xxxxxxxx"))
        assertEquals(0, runs("xxxxxxx"))
        assertEquals(8, runs("xxxx.xxxx"))        // one-frame dip inside a word
        assertEquals(0, runs("xxxx..xxxx"))       // two short sounds
        assertEquals(16, runs("..xxxxxxxx..xxxxxxxx"))
        assertEquals(0, runs("x.x.x.x"))           // span 7 < 8
        assertEquals(5, runs("x.x.x.x.x"))         // span 9: a rattle long enough to count
        assertEquals(0, runs(""))
    }

    @Test fun dbfsScale() {
        assertEquals(0.0, QuietCheck.dbfs(32768.0), 1e-9)
        assertEquals(-6.02, QuietCheck.dbfs(16384.0), 0.01)
        assertEquals(QuietCheck.SILENCE_DBFS, QuietCheck.dbfs(0.0), 0.0)
    }
}

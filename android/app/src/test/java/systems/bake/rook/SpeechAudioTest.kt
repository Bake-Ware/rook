package systems.bake.rook

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

class SpeechAudioTest {
    private fun frame(v: Int) = ByteArray(640) { v.toByte() }

    /** Feeds [speech] frames then [silence] frames; what came out. */
    private fun run(seg: Segmenter, speech: Int, silence: Int, start: Int = 1): List<Segmenter.Out> {
        val out = ArrayList<Segmenter.Out>()
        repeat(speech) { seg.feed(frame(start), true)?.let(out::add) }
        repeat(silence) { seg.feed(frame(0), false)?.let(out::add) }
        return out
    }

    @Test fun anUtteranceKeepsItsPrerollAndEndsOnSilence() {
        val seg = Segmenter()
        repeat(40) { assertNull(seg.feed(frame(9), false)) }         // only the last PREROLL are kept
        val out = run(seg, 12, Segmenter.END_SILENCE)
        assertTrue(out[0] is Segmenter.Out.Started)
        val u = out[1] as Segmenter.Out.Utterance
        assertEquals((Segmenter.PREROLL + 12 + Segmenter.END_SILENCE) * 640, u.pcm.size)
        assertEquals(9, u.pcm[0].toInt())
        assertEquals(1, u.pcm[Segmenter.PREROLL * 640].toInt())
        assertEquals(2, out.size)
        assertTrue(!seg.active)
    }

    @Test fun aClickIsDropped() {
        val seg = Segmenter()
        assertTrue(run(seg, Segmenter.MIN_SPEECH - 1, Segmenter.END_SILENCE).isEmpty())
        // The next real utterance is whole.
        val out = run(seg, 20, Segmenter.END_SILENCE, start = 3)
        assertEquals(2, out.size)
    }

    @Test fun aShortPauseDoesNotEndIt() {
        val seg = Segmenter()
        val out = ArrayList<Segmenter.Out>()
        out += run(seg, 15, Segmenter.END_SILENCE - 1)
        out += run(seg, 15, Segmenter.END_SILENCE)
        assertEquals(2, out.size)     // Started, then one utterance
        assertEquals((30 + 2 * Segmenter.END_SILENCE - 1) * 640, (out[1] as Segmenter.Out.Utterance).pcm.size)
    }

    @Test fun twentySecondsAtMost() {
        val seg = Segmenter()
        val out = run(seg, Segmenter.MAX_FRAMES + 5, 0)
        val u = out.filterIsInstance<Segmenter.Out.Utterance>().single()
        assertEquals(Segmenter.MAX_FRAMES * 640, u.pcm.size)
    }

    @Test fun resetDropsWhatWasHalfHeard() {
        val seg = Segmenter()
        run(seg, 20, 3)
        seg.reset()
        assertTrue(run(seg, 0, Segmenter.END_SILENCE).isEmpty())
    }

    @Test fun wavMono() {
        val pcm = ByteArray(480) { it.toByte() }
        val (rate, got) = Wav.pcm(wav(pcm, 24000))!!
        assertEquals(24000, rate)
        assertArrayEquals(pcm, got)
    }

    @Test fun wavStereoTakesTheFirstChannelAndSkipsOtherChunks() {
        val stereo = byteArrayOf(1, 2, 9, 9, 3, 4, 9, 9)
        val (rate, got) = Wav.pcm(wav(stereo, 16000, channels = 2, extra = true))!!
        assertEquals(16000, rate)
        assertArrayEquals(byteArrayOf(1, 2, 3, 4), got)
    }

    @Test fun notAWav() {
        assertNull(Wav.pcm(ByteArray(0)))
        assertNull(Wav.pcm("RIFF....MP3 nonsense".toByteArray()))
        assertNull(Wav.pcm(wav(ByteArray(8), 16000, bits = 8)))
    }

    companion object {
        fun wav(pcm: ByteArray, rate: Int, channels: Int = 1, bits: Int = 16, extra: Boolean = false): ByteArray {
            val out = ByteArrayOutputStream()
            fun i32(v: Int) = out.write(ByteBuffer.allocate(4).order(ByteOrder.LITTLE_ENDIAN).putInt(v).array())
            fun i16(v: Int) = out.write(ByteBuffer.allocate(2).order(ByteOrder.LITTLE_ENDIAN).putShort(v.toShort()).array())
            out.write("RIFF".toByteArray()); i32(0); out.write("WAVE".toByteArray())
            out.write("fmt ".toByteArray()); i32(16); i16(1); i16(channels); i32(rate)
            i32(rate * channels * bits / 8); i16(channels * bits / 8); i16(bits)
            if (extra) { out.write("LIST".toByteArray()); i32(3); out.write(byteArrayOf(1, 2, 3, 0)) }   // odd size: padded
            out.write("data".toByteArray()); i32(pcm.size); out.write(pcm)
            return out.toByteArray()
        }
    }
}

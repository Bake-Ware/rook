package systems.bake.rook.wearlink

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.sin

class WearLinkTest {
    @Test fun framesRoundTrip() {
        val bytes = ByteArrayOutputStream()
        val w = LinkWriter(bytes)
        w.send(WearLink.MIC, byteArrayOf(1, 2, 3))
        w.send(WearLink.END)
        val r = LinkReader(ByteArrayInputStream(bytes.toByteArray()))
        val first = r.next()!!
        assertEquals(WearLink.MIC, first.type)
        assertArrayEquals(byteArrayOf(1, 2, 3), first.payload)
        val second = r.next()!!
        assertEquals(WearLink.END, second.type)
        assertEquals(0, second.payload.size)
        assertNull(r.next())
    }

    @Test(expected = java.io.IOException::class) fun refusesHugeLength() {
        val bad = byteArrayOf(2, 0x7f, 0, 0, 0)
        LinkReader(ByteArrayInputStream(bad)).next()
    }

    private fun tone(rate: Int, ms: Int, hz: Double = 440.0): ByteArray {
        val n = rate * ms / 1000
        val out = ByteArray(n * 2)
        for (i in 0 until n) {
            val s = (sin(2 * PI * hz * i / rate) * 12000).toInt()
            out[2 * i] = s.toByte(); out[2 * i + 1] = (s shr 8).toByte()
        }
        return out
    }

    private fun frames(rate: Int, chunks: List<ByteArray>): List<ByteArray> {
        val a = FrameAssembler()
        val out = ArrayList<ByteArray>()
        for (c in chunks) a.push(c, rate) { out += it }
        return out
    }

    @Test fun passesSixteenKhzThroughInFrames() {
        val pcm = tone(16000, 100)
        val out = frames(16000, pcm.toList().chunked(334).map { it.toByteArray() })
        assertEquals(5, out.size)
        assertTrue(out.all { it.size == WearLink.FRAME_BYTES })
        assertArrayEquals(pcm.copyOfRange(0, 640), out[0])
    }

    @Test fun downsamplesFortyEightKhz() {
        // 1 s at 48 kHz in 20 ms decoder packets -> about 1 s at 16 kHz (50 frames).
        val pcm = tone(48000, 1000)
        val out = frames(48000, pcm.toList().chunked(1920).map { it.toByteArray() })
        assertTrue("frames=${out.size}", out.size in 49..50)
        // The tone survives: 440 Hz has about 36 samples per period at 16 kHz.
        val all = out.flatMap { f -> (0 until f.size / 2).map { ((f[2 * it].toInt() and 0xff) or (f[2 * it + 1].toInt() shl 8)).toShort().toInt() } }
        assertTrue(all.maxOrNull()!! > 11000)
        val expected = (0 until 200).map { (sin(2 * PI * 440.0 * it / 16000) * 12000).toInt() }
        assertTrue((0 until 200).all { abs(all[it] - expected[it]) < 600 })
    }
}

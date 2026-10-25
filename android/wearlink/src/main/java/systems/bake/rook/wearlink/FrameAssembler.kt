package systems.bake.rook.wearlink

import kotlin.math.floor
import kotlin.math.roundToInt

/**
 * PCM16 mono at any rate in, 16 kHz frames of [WearLink.FRAME_BYTES] out (linear
 * interpolation; plenty for speech recognition). An Opus decoder usually gives 48 kHz.
 */
class FrameAssembler {
    private val frame = ByteArray(WearLink.FRAME_BYTES)
    private var filled = 0
    private var rate = WearLink.SR_IN
    /** Next output position, in input samples from this chunk's first sample (-1: the last chunk's last). */
    private var pos = 0.0
    private var prev = 0

    fun push(pcm: ByteArray, sampleRate: Int, emit: (ByteArray) -> Unit) {
        val n = pcm.size / 2
        if (n == 0 || sampleRate <= 0) return
        if (sampleRate != rate) { rate = sampleRate; pos = 0.0 }
        if (rate == WearLink.SR_IN) {
            for (k in 0 until n) put(sample(pcm, k), emit)
            prev = sample(pcm, n - 1)
            return
        }
        val step = rate.toDouble() / WearLink.SR_IN
        while (true) {
            val i = floor(pos).toInt()
            if (i + 1 >= n) break
            val a = if (i < 0) prev else sample(pcm, i)
            val b = sample(pcm, i + 1)
            put((a + (b - a) * (pos - i)).roundToInt(), emit)
            pos += step
        }
        pos -= n
        prev = sample(pcm, n - 1)
    }

    private fun sample(pcm: ByteArray, k: Int) =
        ((pcm[2 * k].toInt() and 0xff) or (pcm[2 * k + 1].toInt() shl 8)).toShort().toInt()

    private fun put(value: Int, emit: (ByteArray) -> Unit) {
        val s = value.coerceIn(-32768, 32767)
        frame[filled++] = s.toByte()
        frame[filled++] = (s shr 8).toByte()
        if (filled == frame.size) { emit(frame.copyOf()); filled = 0 }
    }
}

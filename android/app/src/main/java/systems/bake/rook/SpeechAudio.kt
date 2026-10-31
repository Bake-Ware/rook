package systems.bake.rook

import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Cuts a stream of 20 ms mic frames into utterances for the 0.5 `speech.hear` cap, which takes
 * one utterance at a time (its end-of-turn check says whether the person only paused).
 * [feed] takes each frame with the on-device VAD's verdict. Pure: the caller owns the threads.
 *
 * An utterance starts at the first speech frame (with [PREROLL] frames before it, so the first
 * syllable isn't clipped), counts once it holds [MIN_SPEECH] speech frames (less is a click or a
 * cough, dropped), and ends after [END_SILENCE] silent frames or at [MAX_FRAMES] (20 s: the
 * hub door takes at most 1 MiB, and base64 L16 grows by a third).
 */
class Segmenter {
    sealed class Out {
        /** The person is talking (enough speech to count). */
        object Started : Out()
        /** A finished utterance: 16 kHz mono PCM16. */
        class Utterance(val pcm: ByteArray) : Out()
    }

    companion object {
        const val PREROLL = 15
        const val MIN_SPEECH = 10
        const val END_SILENCE = 35
        const val MAX_FRAMES = 1000
    }

    private val preroll = ArrayDeque<ByteArray>()
    private val frames = ArrayList<ByteArray>()
    private var speech = 0
    private var silence = 0
    val active get() = frames.isNotEmpty()

    fun feed(frame: ByteArray, isSpeech: Boolean): Out? {
        if (frames.isEmpty()) {
            if (!isSpeech) {
                preroll.addLast(frame); while (preroll.size > PREROLL) preroll.removeFirst()
                return null
            }
            frames.addAll(preroll); preroll.clear()
        }
        frames += frame
        if (isSpeech) { speech++; silence = 0 } else silence++
        if (isSpeech && speech == MIN_SPEECH) return Out.Started
        if (silence >= END_SILENCE || frames.size >= MAX_FRAMES) return end()
        return null
    }

    /** Drops what is half heard (the reply is playing: the mic would hear it). */
    fun reset() { frames.clear(); preroll.clear(); speech = 0; silence = 0 }

    private fun end(): Out? {
        val counts = speech >= MIN_SPEECH
        val out = ByteArrayOutputStream(frames.sumOf { it.size })
        if (counts) frames.forEach { out.write(it) }
        reset()
        return if (counts) Out.Utterance(out.toByteArray()) else null
    }
}

/** The 16-bit PCM WAV clips `voice.tts` returns (`audio/wav`), as (sample rate, mono PCM16). */
object Wav {
    fun pcm(bytes: ByteArray): Pair<Int, ByteArray>? {
        if (bytes.size < 12 || String(bytes, 0, 4, Charsets.US_ASCII) != "RIFF" ||
            String(bytes, 8, 4, Charsets.US_ASCII) != "WAVE") return null
        val b = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN)
        var pos = 12
        var rate = 0; var channels = 0; var bits = 0; var format = 0
        while (pos + 8 <= bytes.size) {
            val id = String(bytes, pos, 4, Charsets.US_ASCII)
            val size = b.getInt(pos + 4)
            val start = pos + 8
            if (size < 0) return null
            when (id) {
                "fmt " -> {
                    if (size < 16 || start + 16 > bytes.size) return null
                    format = b.getShort(start).toInt(); channels = b.getShort(start + 2).toInt()
                    rate = b.getInt(start + 4); bits = b.getShort(start + 14).toInt()
                }
                "data" -> {
                    if (format != 1 || bits != 16 || channels < 1 || rate <= 0) return null
                    val end = minOf(bytes.size, start + size)      // a streamed WAV may say more than it has
                    val data = bytes.copyOfRange(start, end - (end - start) % (2 * channels))
                    return rate to if (channels == 1) data else firstChannel(data, channels)
                }
            }
            pos = start + size + (size and 1)
        }
        return null
    }

    private fun firstChannel(data: ByteArray, channels: Int): ByteArray {
        val step = 2 * channels
        val out = ByteArray(data.size / channels)
        for (i in 0 until data.size / step) { out[2 * i] = data[i * step]; out[2 * i + 1] = data[i * step + 1] }
        return out
    }
}

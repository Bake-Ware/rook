package systems.bake.rook.wearlink

import android.media.MediaCodec
import android.media.MediaFormat
import android.os.Build
import java.nio.ByteBuffer
import java.nio.ByteOrder

/** PCM16 mono in, packets out. [encode] returns the packets ready so far. Own on one thread. */
interface AudioEncoder : AutoCloseable {
    val codec: String
    fun encode(pcm: ByteArray): List<ByteArray>
}

/** Packets in, PCM16 mono out at [outputRate]. Own on one thread. */
interface AudioDecoder : AutoCloseable {
    val codec: String
    val outputRate: Int
    fun decode(packet: ByteArray): List<ByteArray>
}

object Codecs {
    /** Opus through MediaCodec when asked for and this device can; otherwise raw PCM. */
    fun encoder(codec: String, sampleRate: Int): AudioEncoder =
        if (codec == WearLink.CODEC_OPUS) try { OpusEncoder(sampleRate) } catch (_: Exception) { Pcm(sampleRate) }
        else Pcm(sampleRate)

    fun decoder(codec: String, sampleRate: Int): AudioDecoder =
        if (codec == WearLink.CODEC_OPUS) try { OpusDecoder(sampleRate) } catch (_: Exception) { Pcm(sampleRate) }
        else Pcm(sampleRate)
}

/** Raw PCM16: the fallback when a device has no Opus codec. */
class Pcm(rate: Int) : AudioEncoder, AudioDecoder {
    override val codec = WearLink.CODEC_PCM
    override val outputRate = rate
    override fun encode(pcm: ByteArray) = listOf(pcm)
    override fun decode(packet: ByteArray) = listOf(packet)
    override fun close() {}
}

/** Opus at 20 ms frames (MediaCodec's encoder exists from Android 10). */
class OpusEncoder(private val sampleRate: Int, bitRate: Int = 24_000) : AudioEncoder {
    override val codec = WearLink.CODEC_OPUS
    private val mc: MediaCodec
    private val info = MediaCodec.BufferInfo()
    private var samples = 0L

    init {
        check(Build.VERSION.SDK_INT >= 29) { "no Opus encoder before Android 10" }
        val format = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_OPUS, sampleRate, 1).apply {
            setInteger(MediaFormat.KEY_BIT_RATE, bitRate)
            setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 32 * 1024)
        }
        mc = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_OPUS)
        try { mc.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE); mc.start() }
        catch (e: Exception) { mc.release(); throw e }
    }

    override fun encode(pcm: ByteArray): List<ByteArray> {
        val out = ArrayList<ByteArray>()
        var off = 0
        var stalls = 0
        while (off < pcm.size) {
            val i = mc.dequeueInputBuffer(10_000)
            if (i < 0) {
                drain(out)
                if (++stalls > 50) throw IllegalStateException("Opus encoder stalled")
                continue
            }
            val buf = mc.getInputBuffer(i) ?: throw IllegalStateException("no encoder input buffer")
            buf.clear()
            val n = minOf(buf.remaining(), pcm.size - off) and 1.inv()
            buf.put(pcm, off, n)
            mc.queueInputBuffer(i, 0, n, samples * 1_000_000L / sampleRate, 0)
            samples += n / 2
            off += n
            drain(out)
        }
        return out
    }

    private fun drain(out: MutableList<ByteArray>) {
        while (true) {
            val i = mc.dequeueOutputBuffer(info, 0)
            when {
                i >= 0 -> {
                    // The codec-config buffer (Opus header) is not sent: the decoder builds its own.
                    if (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG == 0 && info.size > 0) {
                        val b = mc.getOutputBuffer(i)!!
                        b.position(info.offset); b.limit(info.offset + info.size)
                        out += ByteArray(info.size).also { b.get(it) }
                    }
                    mc.releaseOutputBuffer(i, false)
                }
                i == MediaCodec.INFO_TRY_AGAIN_LATER -> return
                else -> {}   // format or buffers changed: keep draining
            }
        }
    }

    override fun close() {
        try { mc.stop() } catch (_: Exception) {}
        mc.release()
    }
}

/** Opus packets of 20 ms. Output is usually 48 kHz whatever the input rate: read [outputRate]. */
class OpusDecoder(sampleRate: Int) : AudioDecoder {
    override val codec = WearLink.CODEC_OPUS
    @Volatile override var outputRate = 48_000
        private set
    private val mc: MediaCodec
    private val info = MediaCodec.BufferInfo()
    private var pts = 0L

    init {
        val format = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_OPUS, sampleRate, 1).apply {
            setByteBuffer("csd-0", opusHead(sampleRate))
            setByteBuffer("csd-1", nanos(0))            // codec delay
            setByteBuffer("csd-2", nanos(80_000_000))   // seek pre-roll
        }
        mc = MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_AUDIO_OPUS)
        try { mc.configure(format, null, null, 0); mc.start() }
        catch (e: Exception) { mc.release(); throw e }
        try { outputRate = mc.outputFormat.getInteger(MediaFormat.KEY_SAMPLE_RATE) } catch (_: Exception) {}
    }

    override fun decode(packet: ByteArray): List<ByteArray> {
        val out = ArrayList<ByteArray>()
        val i = mc.dequeueInputBuffer(20_000)
        if (i >= 0) {
            val b = mc.getInputBuffer(i)!!
            b.clear(); b.put(packet)
            mc.queueInputBuffer(i, 0, packet.size, pts, 0)
            pts += 20_000
        }   // else the decoder is behind: this packet is dropped
        var timeout = 5_000L
        while (true) {
            val o = mc.dequeueOutputBuffer(info, timeout)
            timeout = 0
            when {
                o >= 0 -> {
                    if (info.size > 0) {
                        val b = mc.getOutputBuffer(o)!!
                        b.position(info.offset); b.limit(info.offset + info.size)
                        out += ByteArray(info.size).also { b.get(it) }
                    }
                    mc.releaseOutputBuffer(o, false)
                }
                o == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED ->
                    try { outputRate = mc.outputFormat.getInteger(MediaFormat.KEY_SAMPLE_RATE) } catch (_: Exception) {}
                o == MediaCodec.INFO_TRY_AGAIN_LATER -> return out
            }
        }
    }

    override fun close() {
        try { mc.stop() } catch (_: Exception) {}
        mc.release()
    }

    private companion object {
        /** RFC 7845 identification header: mono, no pre-skip, no gain, mapping family 0. */
        fun opusHead(sampleRate: Int): ByteBuffer = ByteBuffer.allocate(19).order(ByteOrder.LITTLE_ENDIAN).apply {
            put("OpusHead".toByteArray(Charsets.US_ASCII)); put(1); put(1)
            putShort(0); putInt(sampleRate); putShort(0); put(0)
            flip()
        }
        fun nanos(value: Long): ByteBuffer = ByteBuffer.allocate(8).order(ByteOrder.nativeOrder()).apply { putLong(value); flip() }
    }
}

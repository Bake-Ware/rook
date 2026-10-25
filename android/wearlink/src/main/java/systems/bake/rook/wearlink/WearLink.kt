package systems.bake.rook.wearlink

import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.EOFException
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream

/**
 * The watch <-> phone voice link. One Wear Data Layer channel per conversation carries
 * length-prefixed messages ([type:u8][length:i32][payload]) both ways. The phone runs the
 * voice session (all the network work); the watch is a microphone and a speaker.
 */
object WearLink {
    const val PATH = "/rook/voice"
    /** Declared by the phone app (res/values/wear.xml); the watch looks for a node that has it. */
    const val CAPABILITY = "rook_voice_phone"
    const val VERSION = 1
    const val SR_IN = 16000
    /** 20 ms of 16 kHz mono PCM16: what VoiceClient takes per frame. */
    const val FRAME_BYTES = 640
    const val MAX_PAYLOAD = 64 * 1024

    // watch -> phone
    const val HELLO = 1        // json {v, codec, sr, accepts}
    const val MIC = 2          // one encoded mic packet
    const val END = 3          // the user ended the conversation
    const val STOP = 4         // stop the reply
    const val PLAYED = 5       // json {turn, frames}: how much of that turn's reply has played
    // phone -> watch
    const val READY = 10       // json {uplink, downlink}: the codecs to use; mic packets may flow
    const val STATE = 11       // json {state}: listening, thinking, speaking, ...
    const val HEARD = 12       // json {text}: what the voice server heard
    const val REPLY = 13       // json {text}: a piece of the reply
    const val REPLY_DONE = 14
    const val OUT_FORMAT = 15  // json {codec, sr, turn}: applies to the OUT_AUDIO that follow
    const val OUT_AUDIO = 16
    const val FLUSH = 17       // drop reply audio not played yet (interrupted)
    const val PAUSE = 18       // hold playback (maybe a barge-in)
    const val RESUME = 19
    const val ERROR = 20       // json {msg}
    const val BYE = 21         // json {reason}: the phone ended the conversation

    const val CODEC_OPUS = "opus"
    const val CODEC_PCM = "pcm16"
}

class LinkMessage(val type: Int, val payload: ByteArray) {
    fun json(): JSONObject = try { JSONObject(String(payload, Charsets.UTF_8)) } catch (_: Exception) { JSONObject() }
}

/** Thread-safe: messages from several threads never interleave. */
class LinkWriter(stream: OutputStream) {
    private val out = DataOutputStream(BufferedOutputStream(stream, 8192))

    @Synchronized
    fun send(type: Int, payload: ByteArray = EMPTY) {
        if (payload.size > WearLink.MAX_PAYLOAD) throw IOException("message too large: ${payload.size}")
        out.writeByte(type)
        out.writeInt(payload.size)
        out.write(payload)
        out.flush()
    }

    fun send(type: Int, json: JSONObject) = send(type, json.toString().toByteArray(Charsets.UTF_8))

    fun close() = try { out.close() } catch (_: Exception) {}

    private companion object { val EMPTY = ByteArray(0) }
}

class LinkReader(stream: InputStream) {
    private val input = DataInputStream(BufferedInputStream(stream, 8192))

    /** The next message, or null when the other side closed the channel. */
    fun next(): LinkMessage? {
        val type = try { input.readUnsignedByte() } catch (_: EOFException) { return null }
        val len = input.readInt()
        if (len < 0 || len > WearLink.MAX_PAYLOAD) throw IOException("bad message length $len")
        val payload = ByteArray(len)
        input.readFully(payload)
        return LinkMessage(type, payload)
    }
}

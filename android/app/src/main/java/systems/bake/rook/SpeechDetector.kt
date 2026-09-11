package systems.bake.rook

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import android.content.Context
import java.nio.FloatBuffer
import java.nio.LongBuffer

/** Silero v4 stateful VAD, the model distributed with openWakeWord v0.5.1.
 * Own on the microphone thread. 30 ms frames, normalized PCM16, 16 kHz.
 */
class SpeechDetector(ctx: Context) : AutoCloseable {
    private val env = OrtEnvironment.getEnvironment()
    private val session = OrtSession.SessionOptions().use { options ->
        options.setIntraOpNumThreads(1)
        env.createSession(ctx.assets.open("wakeword/silero_vad.onnx").use { it.readBytes() }, options)
    }
    private var h = FloatArray(128)
    private var c = FloatArray(128)
    private val pending = FloatArray(480)
    private var filled = 0
    var probability = 0f; private set
    private var hangover = 0
    val recentSpeech get() = hangover > 0

    fun feed(bytes: ByteArray): Boolean {
        for (i in bytes.indices step 2) {
            if (i + 1 >= bytes.size) break
            pending[filled++] = ((bytes[i].toInt() and 255) or (bytes[i+1].toInt() shl 8)).toShort() / 32768f
            if (filled == pending.size) {
                OnnxTensor.createTensor(env, FloatBuffer.wrap(pending), longArrayOf(1,480)).use { input ->
                    OnnxTensor.createTensor(env, FloatBuffer.wrap(h), longArrayOf(2,1,64)).use { ht ->
                        OnnxTensor.createTensor(env, FloatBuffer.wrap(c), longArrayOf(2,1,64)).use { ct ->
                            OnnxTensor.createTensor(env, LongBuffer.wrap(longArrayOf(16000)), longArrayOf()).use { sr ->
                                session.run(mapOf("input" to input, "h" to ht, "c" to ct, "sr" to sr)).use { result ->
                                    probability = flatten(result[0].value)[0]
                                    h = flatten(result[1].value); c = flatten(result[2].value)
                                }
                            }
                        }
                    }
                }
                hangover = if (probability >= .6f) 16 else maxOf(0, hangover - 1)
                filled = 0
            }
        }
        return probability >= .6f
    }
    private fun flatten(value: Any?): FloatArray = when (value) {
        is FloatArray -> value
        is Array<*> -> value.flatMap { flatten(it).asList() }.toFloatArray()
        else -> FloatArray(0)
    }
    override fun close() = session.close()
}

package systems.bake.rook

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import android.content.Context
import android.util.Log
import java.nio.FloatBuffer

/**
 * Kotlin port of openWakeWord's streaming pipeline (openwakeword/utils.py AudioFeatures):
 *
 *   int16 PCM @16k  --(1280-sample chunks; melspec run on last 1280+480 samples)-->
 *   mel frames (x/10+2, 32 bins, buffer 970)  --(last 76 frames, [1,76,32,1])-->
 *   96-d embedding (buffer 120)  --(last 16, [1,16,96])-->  wake score in [0,1]
 *
 * Models live in assets/wakeword/: melspectrogram.onnx + embedding_model.onnx (shared
 * openWakeWord feature extractors) and one or more <name>.onnx wake classifiers.
 *
 * Not thread-safe; call [feed] from the mic thread only.
 */
class WakeWordDetector(ctx: Context, private val wakeModelAsset: String, private val threshold: Float = 0.5f) {

    companion object {
        private const val TAG = "WakeWord"
        const val CHUNK = 1280           // 80 ms
        private const val MEL_CTX = 160 * 3
        private const val MEL_BINS = 32
        private const val MEL_MAX = 970
        private const val EMB_WIN = 76
        private const val EMB_DIM = 96
        private const val EMB_MAX = 120
        private const val N_FRAMES = 16
    }

    private val env = OrtEnvironment.getEnvironment()
    private val melSess: OrtSession
    private val embSess: OrtSession
    private val wakeSess: OrtSession
    private val melIn: String; private val embIn: String; private val wakeIn: String

    // rolling raw audio (int16 as float) — keep a little more than CHUNK+MEL_CTX
    private val raw = FloatArray(CHUNK + MEL_CTX)
    private var rawFilled = 0
    private val pending = FloatArray(CHUNK)
    private var pendingLen = 0

    private val mel = ArrayDeque<FloatArray>()      // each = 32 bins
    private val emb = ArrayDeque<FloatArray>()      // each = 96
    @Volatile var lastScore = 0f; private set

    init {
        fun load(name: String): OrtSession {
            val bytes = ctx.assets.open("wakeword/$name").use { it.readBytes() }
            val opts = OrtSession.SessionOptions().apply { setIntraOpNumThreads(1) }
            return env.createSession(bytes, opts)
        }
        melSess = load("melspectrogram.onnx")
        embSess = load("embedding_model.onnx")
        wakeSess = load(wakeModelAsset)
        melIn = melSess.inputNames.first(); embIn = embSess.inputNames.first(); wakeIn = wakeSess.inputNames.first()
        Log.i(TAG, "loaded $wakeModelAsset (mel in=$melIn emb in=$embIn wake in=$wakeIn)")
    }

    /** Feed PCM16 LE bytes (any length). Returns true once per detection (with refractory handled by caller). */
    fun feed(pcm: ByteArray, len: Int = pcm.size): Boolean {
        var hit = false
        var i = 0
        while (i + 1 < len) {
            val s = ((pcm[i].toInt() and 0xff) or (pcm[i + 1].toInt() shl 8)).toShort().toFloat()
            pending[pendingLen++] = s
            i += 2
            if (pendingLen == CHUNK) {
                if (processChunk()) hit = true
                pendingLen = 0
            }
        }
        return hit
    }

    private fun processChunk(): Boolean {
        // shift raw buffer left by CHUNK, append pending
        val keep = raw.size - CHUNK
        System.arraycopy(raw, CHUNK, raw, 0, keep)
        System.arraycopy(pending, 0, raw, keep, CHUNK)
        rawFilled = minOf(raw.size, rawFilled + CHUNK)
        if (rawFilled < 400) return false
        val n = minOf(rawFilled, CHUNK + MEL_CTX)
        val seg = raw.copyOfRange(raw.size - n, raw.size)

        // 1) melspectrogram on the last CHUNK+480 samples
        OnnxTensor.createTensor(env, FloatBuffer.wrap(seg), longArrayOf(1, n.toLong())).use { t ->
            melSess.run(mapOf(melIn to t)).use { r ->
                val out = r[0].value
                val flat = flatten(out)
                val frames = flat.size / MEL_BINS
                for (f in 0 until frames) {
                    val row = FloatArray(MEL_BINS)
                    for (b in 0 until MEL_BINS) row[b] = flat[f * MEL_BINS + b] / 10f + 2f
                    mel.addLast(row)
                }
                while (mel.size > MEL_MAX) mel.removeFirst()
            }
        }
        if (mel.size < EMB_WIN) return false

        // 2) embedding from the last 76 mel frames -> [1,76,32,1]
        val win = FloatArray(EMB_WIN * MEL_BINS)
        var k = 0
        val start = mel.size - EMB_WIN
        for ((idx, row) in mel.withIndex()) if (idx >= start) { System.arraycopy(row, 0, win, k, MEL_BINS); k += MEL_BINS }
        OnnxTensor.createTensor(env, FloatBuffer.wrap(win), longArrayOf(1, EMB_WIN.toLong(), MEL_BINS.toLong(), 1)).use { t ->
            embSess.run(mapOf(embIn to t)).use { r ->
                emb.addLast(flatten(r[0].value).copyOf(EMB_DIM))
                while (emb.size > EMB_MAX) emb.removeFirst()
            }
        }
        if (emb.size < N_FRAMES) return false

        // 3) wake classifier on the last 16 embeddings -> [1,16,96]
        val x = FloatArray(N_FRAMES * EMB_DIM)
        k = 0
        val s2 = emb.size - N_FRAMES
        for ((idx, e) in emb.withIndex()) if (idx >= s2) { System.arraycopy(e, 0, x, k, EMB_DIM); k += EMB_DIM }
        OnnxTensor.createTensor(env, FloatBuffer.wrap(x), longArrayOf(1, N_FRAMES.toLong(), EMB_DIM.toLong())).use { t ->
            wakeSess.run(mapOf(wakeIn to t)).use { r ->
                lastScore = flatten(r[0].value)[0]
            }
        }
        return lastScore >= threshold
    }

    /** Reset temporal buffers (e.g. after a detection so it doesn't re-fire on the tail). */
    fun reset() { mel.clear(); emb.clear(); lastScore = 0f }

    fun close() { try { melSess.close(); embSess.close(); wakeSess.close() } catch (_: Throwable) {} }

    private fun flatten(v: Any?): FloatArray = when (v) {
        is FloatArray -> v
        is Array<*> -> v.flatMap { flatten(it).asList() }.toFloatArray()
        else -> FloatArray(0)
    }
}

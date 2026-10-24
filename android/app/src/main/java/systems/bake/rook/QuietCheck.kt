package systems.bake.rook

import kotlin.math.log10
import kotlin.math.max
import kotlin.math.roundToInt
import kotlin.math.sqrt

/**
 * Is the room quiet enough to speak a reminder? Pure Kotlin (no Android types), unit-tested on
 * the JVM. [SpokenReminder] feeds it a few seconds of 16 kHz mono PCM16 that never leaves memory;
 * it keeps only one level number per 30 ms frame and the samples are dropped as they are read.
 *
 * The rule, per sample:
 *  1. Frame the audio in 30 ms frames and take each frame's RMS level in dBFS.
 *  2. Digital silence (every sample of nearly every frame exactly 0) means Android silenced the
 *     capture (a background app, another app holding the mic): [Outcome.SILENCED], we can't tell.
 *  3. The sample's own noise floor is its 20th-percentile frame level. A frame is *active* when
 *     it is louder than [ABS_FLOOR_DBFS] and at least [MARGIN_DB] above that floor.
 *  4. Active frames only count inside a run of at least [MIN_RUN_FRAMES] (240 ms; gaps of up to
 *     [GAP_FRAMES] frame are bridged, as between syllables). A clap, a door or a cough is one or
 *     two frames and never makes a run.
 *  5. Busy when the counted frames add up to [BUSY_ACTIVE_FRAMES] (300 ms) or more: someone talked.
 *  6. Busy also when the whole sample is loud and fluctuating (median at least [LOUD_DBFS] and a
 *     spread p90 - p10 of at least [MODULATION_DB]): a TV or a conversation that raised the noise
 *     floor itself, so step 3 alone would miss it. A steady hum has almost no spread and stays
 *     quiet, unless it is very loud ([VERY_LOUD_DBFS] median: nobody would hear the reminder).
 *
 * The numbers are for a phone's microphone at arm's length to across the room: normal speech near
 * the phone reads around -35 to -20 dBFS, a quiet room -70 to -55 dBFS, a fridge or fan hum
 * -60 to -45 dBFS with a spread of a few dB.
 */
class QuietCheck(private val sampleRate: Int = SAMPLE_RATE) {
    enum class Outcome { QUIET, BUSY, SILENCED }

    /** The verdict plus the only numbers that may leave the phone (rounded summaries, never audio). */
    data class Result(val outcome: Outcome, val activeFrac: Double, val peakDbfs: Int, val frames: Int)

    private val frameLen = sampleRate * FRAME_MS / 1000
    private val levels = ArrayList<Double>()
    private var zeroFrames = 0
    private var sumSq = 0.0
    private var nonZero = false
    private var filled = 0

    /** Total frames measured so far. */
    val frames: Int get() = levels.size
    /** Milliseconds of audio measured so far. */
    val measuredMs: Int get() = levels.size * FRAME_MS

    /** Feed [count] samples from [pcm] (16-bit signed). Any chunk size; framing is internal. */
    fun feed(pcm: ShortArray, count: Int = pcm.size) {
        for (i in 0 until count.coerceIn(0, pcm.size)) push(pcm[i].toInt())
    }

    /** Feed little-endian PCM16 bytes (what AudioRecord.read(ByteArray) gives). */
    fun feedBytes(pcm: ByteArray, count: Int = pcm.size) {
        val n = count.coerceIn(0, pcm.size) and 1.inv()
        var i = 0
        while (i < n) {
            push((pcm[i].toInt() and 0xff) or (pcm[i + 1].toInt() shl 8))
            i += 2
        }
    }

    private fun push(sample: Int) {
        if (sample != 0) nonZero = true
        sumSq += sample.toDouble() * sample
        if (++filled == frameLen) {
            if (!nonZero) { zeroFrames++; levels.add(SILENCE_DBFS) }
            else levels.add(dbfs(sqrt(sumSq / frameLen)))
            filled = 0; sumSq = 0.0; nonZero = false
        }
    }

    fun result(): Result {
        val n = levels.size
        if (n < MIN_FRAMES || zeroFrames * 100 >= n * SILENCED_PCT) {
            return Result(Outcome.SILENCED, 0.0, peak(), n)
        }
        val live = levels.filter { it > SILENCE_DBFS }.sorted()
        val floor = percentile(live, 0.20)
        val median = percentile(live, 0.50)
        val spread = percentile(live, 0.90) - percentile(live, 0.10)
        val threshold = max(ABS_FLOOR_DBFS, floor + MARGIN_DB)
        val active = BooleanArray(n) { levels[it] >= threshold }
        val activeCount = active.count { it }
        val counted = countedRuns(active)
        val busy = counted >= BUSY_ACTIVE_FRAMES ||
            (median >= LOUD_DBFS && spread >= MODULATION_DB) ||
            median >= VERY_LOUD_DBFS
        val frac = (activeCount.toDouble() / n * 100).roundToInt() / 100.0
        return Result(if (busy) Outcome.BUSY else Outcome.QUIET, frac, peak(), n)
    }

    private fun peak(): Int = (levels.maxOrNull() ?: SILENCE_DBFS).roundToInt()

    companion object {
        const val SAMPLE_RATE = 16_000
        const val FRAME_MS = 30
        /** Level used for an all-zero frame (log of 0). */
        const val SILENCE_DBFS = -120.0
        /** Too short to judge (under 0.9 s measured): treated as not checked. */
        const val MIN_FRAMES = 30
        /** At least this share of frames all-zero: the platform silenced the capture. */
        const val SILENCED_PCT = 90
        /** An active frame is never quieter than this, however quiet the room. */
        const val ABS_FLOOR_DBFS = -50.0
        /** ...and stands this far above the sample's own noise floor (its 20th percentile). */
        const val MARGIN_DB = 10.0
        /** A run of active frames must last this long to count (8 x 30 ms = 240 ms). */
        const val MIN_RUN_FRAMES = 8
        /** Quiet frames bridged inside a run (the dip between syllables). */
        const val GAP_FRAMES = 1
        /** Counted active time that makes the room busy (10 x 30 ms = 300 ms). */
        const val BUSY_ACTIVE_FRAMES = 10
        /** A sample this loud on the median... */
        const val LOUD_DBFS = -40.0
        /** ...that also fluctuates this much (p90 - p10) is a TV or a conversation. */
        const val MODULATION_DB = 6.0
        /** So loud on the median that a reminder would not be heard, steady or not. */
        const val VERY_LOUD_DBFS = -25.0

        fun dbfs(rms: Double): Double = if (rms <= 0.0) SILENCE_DBFS else max(SILENCE_DBFS, 20 * log10(rms / 32768.0))

        internal fun percentile(sorted: List<Double>, p: Double): Double {
            if (sorted.isEmpty()) return SILENCE_DBFS
            val i = ((sorted.size - 1) * p).roundToInt().coerceIn(0, sorted.size - 1)
            return sorted[i]
        }

        /** Active frames that sit in runs of at least [MIN_RUN_FRAMES] (bridging [GAP_FRAMES]). */
        internal fun countedRuns(active: BooleanArray): Int {
            var total = 0
            var i = 0
            val n = active.size
            while (i < n) {
                if (!active[i]) { i++; continue }
                var end = i          // last active frame of this run
                var j = i + 1
                var activeInRun = 1
                while (j < n) {
                    if (active[j]) { end = j; activeInRun++; j++; continue }
                    // A gap: bridge it only if activity resumes after at most GAP_FRAMES quiet frames.
                    var k = j
                    while (k < n && !active[k]) k++
                    if (k < n && k - end - 1 <= GAP_FRAMES) { j = k; continue }
                    break
                }
                if (end - i + 1 >= MIN_RUN_FRAMES) total += activeInRun
                i = end + 1
            }
            return total
        }
    }
}

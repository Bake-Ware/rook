package systems.bake.rook.wear

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.os.SystemClock
import android.util.Log
import systems.bake.rook.wearlink.AudioDecoder
import systems.bake.rook.wearlink.Codecs
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/**
 * Plays the replies the phone sends, on the watch's audio output: paired earbuds or glasses
 * when connected (media route), else the watch speaker. [format], [audio], [flush], [pause]
 * and [resume] come from the link's reader thread; [played] reports progress for the server,
 * [playing] whether reply audio is coming out of the watch right now (on changes only).
 */
class ReplyPlayer(
    private val played: (turn: Int, frames: Long) -> Unit,
    private val playing: (Boolean) -> Unit = {},
) : AutoCloseable {
    private sealed class Item {
        class Turn(val turn: Int, val sr: Int) : Item()
        class Pcm(val bytes: ByteArray, val rate: Int) : Item()
        object Flush : Item()
    }

    private val queue = LinkedBlockingQueue<Item>(2048)
    private val lock = Any()
    @Volatile private var running = true
    @Volatile private var paused = false
    private var decoder: AudioDecoder? = null
    private var codec = ""
    private var sr = 0
    private var turn = -1
    // Player thread only.
    private var track: AudioTrack? = null
    private var trackRate = 0
    private var playTurn = -1
    private var streamSr = 24000
    private var turnStart = 0L
    private var lastReport = 0L
    /** When the audio written so far will have played out (elapsedRealtime); 0: nothing pending. */
    @Volatile private var endsAt = 0L
    @Volatile private var pausedAt = 0L
    private var wasPlaying = false
    private val player = thread(name = "reply-player") { play() }

    fun format(codec: String, sampleRate: Int, turn: Int) {
        if (codec != this.codec || sampleRate != sr || decoder == null) {
            decoder?.close()
            decoder = Codecs.decoder(codec, sampleRate)
            this.codec = codec; sr = sampleRate
        }
        if (turn != this.turn) { this.turn = turn; queue.offer(Item.Turn(turn, sampleRate)) }
    }

    fun audio(packet: ByteArray) {
        val d = decoder ?: return
        for (pcm in d.decode(packet)) if (pcm.isNotEmpty() && !queue.offer(Item.Pcm(pcm, d.outputRate))) Log.w(TAG, "reply queue full")
    }

    fun flush() {
        val it = queue.iterator()
        while (it.hasNext()) if (it.next() is Item.Pcm) it.remove()
        queue.offer(Item.Flush)
        paused = false
    }

    fun pause() = synchronized(lock) {
        if (!paused) pausedAt = SystemClock.elapsedRealtime()
        paused = true; try { track?.pause() } catch (_: Exception) {}
    }
    fun resume() = synchronized(lock) {
        // Paused audio plays later by as long as it was held.
        if (paused && endsAt > 0) endsAt += SystemClock.elapsedRealtime() - pausedAt
        paused = false; try { track?.play() } catch (_: Exception) {}
    }

    private fun play() {
        try {
            while (running) {
                val item = queue.poll(100, TimeUnit.MILLISECONDS)
                report()
                notePlaying()
                when (item) {
                    null -> {}
                    is Item.Turn -> {
                        // A new reply cuts what is left of the last one (as the phone's player does).
                        synchronized(lock) { track?.let { it.pause(); it.flush(); if (!paused) it.play() } }
                        endsAt = 0L
                        playTurn = item.turn; streamSr = item.sr
                        turnStart = track?.playbackHeadPosition?.toLong()?.and(0xffffffffL) ?: 0L
                    }
                    is Item.Flush -> synchronized(lock) {
                        track?.let { it.pause(); it.flush(); it.play() }
                        endsAt = 0L
                        turnStart = track?.playbackHeadPosition?.toLong()?.and(0xffffffffL) ?: 0L
                    }
                    is Item.Pcm -> write(item)
                }
            }
        } catch (_: InterruptedException) {
        } catch (e: Exception) { Log.w(TAG, "reply playback failed", e) }
        finally {
            synchronized(lock) { try { track?.stop(); track?.release() } catch (_: Exception) {}; track = null }
            endsAt = 0L; notePlaying()
        }
    }

    /** Player thread: tells [playing] when sound starts or stops coming out. */
    private fun notePlaying() {
        val now = !paused && SystemClock.elapsedRealtime() < endsAt
        if (now != wasPlaying) { wasPlaying = now; playing(now) }
    }

    private fun write(item: Item.Pcm) {
        var t = track
        if (t == null || trackRate != item.rate) {
            synchronized(lock) {
                t?.release()
                t = AudioTrack.Builder()
                    .setAudioAttributes(AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                    .setAudioFormat(AudioFormat.Builder().setSampleRate(item.rate).setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
                    .setBufferSizeInBytes(maxOf(AudioTrack.getMinBufferSize(item.rate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT), item.rate / 5))
                    .setTransferMode(AudioTrack.MODE_STREAM).build()
                track = t; trackRate = item.rate; turnStart = 0L
                if (!paused) t!!.play()
            }
        }
        val tr = t!!
        var off = 0
        while (running && off < item.bytes.size) {
            if (queue.peek() is Item.Flush) return
            val n = tr.write(item.bytes, off, item.bytes.size - off, AudioTrack.WRITE_NON_BLOCKING)
            if (n < 0) return
            if (n == 0) { Thread.sleep(5); report() } else {
                off += n
                val now = SystemClock.elapsedRealtime()
                endsAt = maxOf(endsAt, now) + n / 2 * 1000L / item.rate
                notePlaying()
            }
        }
    }

    /** Progress of the reply now playing, at the reply's own sample rate (what the server counts). */
    private fun report() {
        val t = track ?: return
        if (playTurn < 0 || trackRate <= 0) return
        val now = SystemClock.elapsedRealtime()
        if (now - lastReport < 200) return
        lastReport = now
        val head = t.playbackHeadPosition.toLong() and 0xffffffffL
        val frames = (head - turnStart).coerceAtLeast(0L) * streamSr / trackRate
        played(playTurn, frames)
    }

    override fun close() {
        running = false
        player.interrupt()
        decoder?.close(); decoder = null
    }

    private companion object { const val TAG = "ReplyPlayer" }
}

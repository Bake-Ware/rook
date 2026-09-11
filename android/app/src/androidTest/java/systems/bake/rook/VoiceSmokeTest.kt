package systems.bake.rook

import android.content.Context
import android.os.Bundle
import kotlin.random.Random

object VoiceSmokeTest {
    fun run(target: Context, test: Context): Bundle {
        val result = Bundle()
        SpeechDetector(target).use { speech ->
            val wake = WakeWordDetector(target, VoiceService.WAKE_MODEL)
            try {
                val rng = Random(42)
                var noiseWakes = 0
                var speechFrames = 0
                var wakeHits = 0
                for (frame in 0 until 1500) {
                    val pcm = ByteArray(640)
                    if (frame >= 500) for (i in pcm.indices step 2) {
                        val sample = if (frame < 1000) rng.nextInt(-400,401) else rng.nextInt(-4000,4001)
                        pcm[i]=sample.toByte();pcm[i+1]=(sample shr 8).toByte()
                    }
                    speech.feed(pcm)
                    if (wake.feed(pcm,speech=speech.recentSpeech)) { noiseWakes++;wake.reset() }
                }
                check(noiseWakes == 0) { "false wakes on synthetic silence/noise: $noiseWakes" }
                wake.reset()
                val fixture = test.assets.open("voice_speech.pcm").use { it.readBytes() }
                for (i in fixture.indices step 640) {
                    val pcm=fixture.copyOfRange(i,minOf(i+640,fixture.size)).copyOf(640)
                    if (speech.feed(pcm)) speechFrames++
                    if (wake.feed(pcm,speech=speech.recentSpeech)) { wakeHits++;wake.reset() }
                }
                check(speechFrames > 10) { "speech gate rejected the spoken fixture" }
                result.putString("stream", "PASS: 30 seconds synthetic silence/noise, zero false wakes; $speechFrames speech frames; $wakeHits synthetic wake detections\n")
            } finally { wake.close() }
        }
        return result
    }
}

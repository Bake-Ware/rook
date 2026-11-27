package systems.bake.rook.core

import java.io.File
import java.io.RandomAccessFile

/**
 * The hub address stamped into this APK by the hub that handed it out (brief AP1).
 *
 * The hub puts it in one ID-value pair of the APK Signing Block, the block just before the zip's
 * central directory, which the APK's signatures do not cover (hub/rook_hub/site.py, stamp_apk).
 * This reads it back: the end record gives the central directory's offset, the 16-byte magic
 * before it says a signing block is there, and the block's pairs are walked for ours.
 * Pure JVM, so it is unit tested.
 */
object HubStamp {
    /** The pair the address is in: "rook" as a little-endian uint32 (site.py HUB_ADDRESS_ID). */
    const val ID = 0x6b6f6f72
    private val MAGIC = "APK Sig Block 42".toByteArray(Charsets.US_ASCII)
    private const val END_RECORD = 0x06054b50
    private const val END_MAX = 22 + 0xffff     // the end record and the longest comment it may carry

    /** The stamped address, or null for an APK with none (a sideloaded build). */
    fun read(apk: File): String? = RandomAccessFile(apk, "r").use { read(it) }

    fun read(file: RandomAccessFile): String? {
        val length = file.length()
        val tail = ByteArray(minOf(length, END_MAX.toLong()).toInt())
        file.seek(length - tail.size)
        file.readFully(tail)
        val end = (tail.size - 22 downTo 0).firstOrNull { le32(tail, it) == END_RECORD } ?: return null
        val directory = le32(tail, end + 16).toLong() and 0xffffffffL
        if (directory < 32) return null
        val foot = ByteArray(24)
        file.seek(directory - 24)
        file.readFully(foot)
        if (!foot.copyOfRange(8, 24).contentEquals(MAGIC)) return null
        val size = le64(foot, 0)
        val pairs = ByteArray((size - 24).toInt())
        file.seek(directory - 8 - size + 8)
        file.readFully(pairs)
        var at = 0
        while (at + 12 <= pairs.size) {
            val pair = le64(pairs, at).toInt()
            if (le32(pairs, at + 8) == ID) return String(pairs, at + 12, pair - 4, Charsets.UTF_8)
            at += 8 + pair
        }
        return null
    }

    private fun le32(b: ByteArray, at: Int): Int =
        (b[at].toInt() and 0xff) or ((b[at + 1].toInt() and 0xff) shl 8) or
            ((b[at + 2].toInt() and 0xff) shl 16) or ((b[at + 3].toInt() and 0xff) shl 24)

    private fun le64(b: ByteArray, at: Int): Long =
        (le32(b, at).toLong() and 0xffffffffL) or (le32(b, at + 4).toLong() shl 32)
}

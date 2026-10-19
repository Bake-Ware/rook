package systems.bake.rook

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import org.json.JSONObject
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * The device unlock PIN, entered in the app's Settings screen, so device.unlock
 * (and passive keep-awake) can unlock without the PIN crossing the band.
 *
 * Encrypted with AES-GCM under a non-exportable Android Keystore key and kept in
 * its own preferences file (app backup is off). The PIN never leaves this class:
 * Python asks [tapStored] to type it on the keypad, and only gets back counts.
 * Nothing here logs it.
 */
object UnlockPin {
    private const val KEY_ALIAS = "rook_unlock_pin"
    private const val PREFS = "rook_unlock"
    private const val CT = "pin_ct"
    private const val IV = "pin_iv"
    private const val AUTO_SUSPENDED = "auto_suspended"

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    /** 4 to 16 digits. */
    @JvmStatic fun valid(pin: String): Boolean = pin.length in 4..16 && pin.all { it in '0'..'9' }

    @JvmStatic fun isSet(ctx: Context): Boolean = prefs(ctx).contains(CT)

    @JvmStatic fun save(ctx: Context, pin: String): Boolean {
        if (!valid(pin)) return false
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.ENCRYPT_MODE, key())
        val ct = c.doFinal(pin.toByteArray(Charsets.UTF_8))
        return prefs(ctx).edit()
            .putString(CT, Base64.encodeToString(ct, Base64.NO_WRAP))
            .putString(IV, Base64.encodeToString(c.iv, Base64.NO_WRAP))
            .remove(AUTO_SUSPENDED)
            .commit()
    }

    @JvmStatic fun clear(ctx: Context) {
        prefs(ctx).edit().clear().commit()
        try {
            KeyStore.getInstance("AndroidKeyStore").apply { load(null) }.deleteEntry(KEY_ALIAS)
        } catch (_: Exception) {}
    }

    /**
     * Passive keep-awake may unlock with the stored PIN unless an earlier
     * automatic attempt left the phone locked: retrying a wrong PIN would run
     * into the lockout (or a wipe policy), so that stops until the PIN is saved
     * again or an explicit device.unlock succeeds.
     */
    @JvmStatic fun autoUnlockAllowed(ctx: Context): Boolean =
        isSet(ctx) && !prefs(ctx).getBoolean(AUTO_SUSPENDED, false)

    @JvmStatic fun noteUnlockResult(ctx: Context, automatic: Boolean, unlocked: Boolean) {
        if (unlocked) prefs(ctx).edit().remove(AUTO_SUSPENDED).apply()
        else if (automatic) prefs(ctx).edit().putBoolean(AUTO_SUSPENDED, true).apply()
    }

    @JvmStatic fun status(ctx: Context): String = JSONObject()
        .put("ok", true)
        .put("pin_set", isSet(ctx))
        .put("auto_unlock", autoUnlockAllowed(ctx))
        .toString()

    /**
     * Type the stored PIN on a 3-column keypad (digits 1-9 in rows, 0 under 8),
     * through the accessibility service. Blocking; run off the main thread.
     * Returns only how many digits were tapped.
     */
    @JvmStatic fun tapStored(ctx: Context, x1: Int, x2: Int, x3: Int,
                             y1: Int, y2: Int, y3: Int, y4: Int, gapMs: Long): String {
        val out = JSONObject()
        val pin = try { read(ctx) } catch (e: Exception) { null }
            ?: return out.put("ok", false).put("error", "no unlock PIN stored (set it in the Rook app's Settings)").toString()
        val cols = intArrayOf(x1, x2, x3)
        val rows = intArrayOf(y1, y2, y3, y4)
        var ok = 0
        for (ch in pin) {
            val d = ch - '0'
            val (x, y) = if (d == 0) cols[1] to rows[3] else cols[(d - 1) % 3] to rows[(d - 1) / 3]
            if (HidAccessibilityService.tap(x, y)) ok++
            Thread.sleep(gapMs)
        }
        return out.put("ok", ok == pin.length).put("digits", pin.length).put("taps_ok", ok).toString()
    }

    private fun read(ctx: Context): String? {
        val p = prefs(ctx)
        val ct = p.getString(CT, null) ?: return null
        val iv = p.getString(IV, null) ?: return null
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, Base64.decode(iv, Base64.NO_WRAP)))
        return String(c.doFinal(Base64.decode(ct, Base64.NO_WRAP)), Charsets.UTF_8)
    }

    private fun key(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(KeyGenParameterSpec.Builder(KEY_ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setKeySize(256)
            .build())
        return gen.generateKey()
    }
}

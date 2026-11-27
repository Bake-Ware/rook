package systems.bake.rook.core.worker

import android.app.Application
import android.content.Context
import android.os.UserManager
import android.util.Log
import java.io.File

/**
 * Storage Android can read before the phone is unlocked for the first time after it restarts (brief AP8):
 * the app's device-protected storage. Everything the worker keeps lives here, so that after a reboot it
 * links to its hub as itself before anyone has unlocked the phone, and phone.unlock can then unlock it:
 *
 * - the worker's state directory (`files/worker`): its keys and identity, and the data its plugins keep;
 * - its preferences (`rook-worker`): the hub's address, the join code until it has joined, on or off;
 * - the phone's cards (`files/notification-android`, [Cards]);
 * - the events not yet sent to the hub and what was last told of the inbox and the notifications (`files/events`,
 *   [Events]), so an event made before the first unlock is kept and nothing is told twice;
 * - Python itself: Chaquopy's unpacked runtime, the worker's code, Python's HOME and temporary files ([python]).
 *
 * Device-protected storage is less protected at rest than the app's ordinary (credential-protected)
 * storage: Android encrypts it with a key that does not wait for the PIN. Bake chose that (27 November).
 * The app itself (the web view, its cookies, its join) stays in ordinary storage.
 *
 * Every other piece of code asks this for the place; none works it out itself. An install whose worker
 * kept its state in ordinary storage (built before AP8) has it moved here once, the first time this is
 * asked while the phone is unlocked ([context]): nothing is lost and the worker does not join again.
 */
object Storage {
    private const val TAG = "RookStorage"
    @Volatile private var moved = false

    /** What the worker keeps in files, by folder: moved here from ordinary storage once. */
    internal val FOLDERS = listOf("worker", "notification-android", "events")
    /** And in preferences. */
    internal val PREFERENCES = listOf("rook-worker")

    /** The app's device-protected storage, with what an older build kept in ordinary storage moved into it once. */
    fun context(context: Context): Context {
        val app = context.applicationContext ?: context
        val device = app.createDeviceProtectedStorageContext()
        if (!moved && unlocked(app)) synchronized(this) {
            if (!moved) {
                moveIn(app, device)
                moved = true
            }
        }
        return device
    }

    /** The files folder Android can read before the first unlock. */
    fun files(context: Context): File = context(context).filesDir

    /** Preferences Android can read before the first unlock. */
    fun prefs(context: Context, name: String) = context(context).getSharedPreferences(name, Context.MODE_PRIVATE)

    /** Whether the phone has been unlocked since it restarted (ordinary storage, and the apps that live in it, are open). */
    fun unlocked(context: Context): Boolean = context.getSystemService(UserManager::class.java).isUserUnlocked

    /**
     * What Chaquopy is started with: an Application over device-protected storage. Chaquopy takes every place
     * from the Application it is given (where it unpacks Python and the worker's code, its own preferences,
     * Python's HOME and temporary files), so all of them are here and Python starts before the first unlock.
     * It is used for nothing else; the caps reach Android through the real application ([Bridge.context]).
     */
    fun python(context: Context): Context = BeforeUnlock(context(context))

    private class BeforeUnlock(storage: Context) : Application() {
        init { attachBaseContext(storage) }
        override fun getApplicationContext(): Context = this
    }

    /** Why a cap cannot do what it needs before the phone has been unlocked since it restarted: Android `what` until then
     *  (the worker puts "<cap> failed: " in front). */
    fun locked(what: String) =
        "this phone has not been unlocked since it restarted, and until it is, Android $what. " +
            "phone.unlock unlocks it (pin: secret:<name>, or the worker's unlock_pin); then call again."

    /** Move what the worker kept in ordinary storage (`from`) into device-protected storage (`to`); the
     *  copy Chaquopy unpacked there goes, since Python now unpacks itself here. */
    private fun moveIn(from: Context, to: Context) {
        try {
            for (name in PREFERENCES) {
                if (File(from.dataDir, "shared_prefs/$name.xml").exists() && !to.moveSharedPreferencesFrom(from, name)) {
                    Log.e(TAG, "the preferences $name could not be moved")
                }
            }
            moveFolders(from.filesDir, to.filesDir, FOLDERS)
            File(from.filesDir, "chaquopy").deleteRecursively()
            File(from.cacheDir, "chaquopy").deleteRecursively()
            from.deleteSharedPreferences("chaquopy")
        } catch (e: Exception) {
            Log.e(TAG, "moving the worker's state", e)
        }
    }

    /**
     * Move each folder in `names` from `from` to `to`. What `from` has replaces what `to` has: it is copied
     * beside its place first, put in place by a rename, and only then deleted from `from`, so a move cut short
     * is done again, whole, the next time. Not a rename across the two: Android's two storages are encrypted
     * with different keys, and Linux renames nothing from one to the other.
     */
    internal fun moveFolders(from: File, to: File, names: List<String>) {
        for (name in names) {
            val old = File(from, name)
            if (!old.exists()) continue
            val place = File(to, name)
            val beside = File(to, "$name.moving")
            beside.deleteRecursively()
            to.mkdirs()
            if (!old.copyRecursively(beside)) throw java.io.IOException("could not copy $old to $beside")
            place.deleteRecursively()
            if (!beside.renameTo(place)) throw java.io.IOException("could not put $beside in place of $place")
            old.deleteRecursively()
        }
    }
}

package systems.bake.rook.core.worker

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/**
 * Moving an older install's state from the app's ordinary storage into the storage Android reads before
 * the first unlock (brief AP8): [Storage.moveFolders], on the JVM, with two folders standing in for the two
 * storages. On the phone, the same function runs over `files` in each.
 */
class StorageTest {
    @get:Rule val tmp = TemporaryFolder()

    private fun storages(): Pair<File, File> = tmp.newFolder("ordinary") to tmp.newFolder("device")

    private fun write(root: File, path: String, text: String) =
        File(root, path).apply { parentFile.mkdirs(); writeText(text) }

    private fun tree(root: File): Map<String, String> =
        root.walkTopDown().filter { it.isFile }.associate { it.relativeTo(root).path to it.readText() }

    @Test fun the_workers_state_moves_whole_and_leaves_ordinary_storage() {
        val (ordinary, device) = storages()
        write(ordinary, "worker/identity.json", """{"moniker": "honest-witty-badger"}""")
        write(ordinary, "worker/key", "k")
        write(ordinary, "worker/link_key", "l")
        write(ordinary, "worker/plugins/phone/kept.json", "{}")
        write(ordinary, "notification-android/cards.json", "{}")
        write(ordinary, "events/unsent.jsonl", "{\"name\": \"sms.received\"}\n")
        write(ordinary, "events/sms-told.json", "{}")

        Storage.moveFolders(ordinary, device, Storage.FOLDERS)

        assertEquals(mapOf(
            "worker/identity.json" to """{"moniker": "honest-witty-badger"}""", "worker/key" to "k", "worker/link_key" to "l",
            "worker/plugins/phone/kept.json" to "{}", "notification-android/cards.json" to "{}",
            "events/unsent.jsonl" to "{\"name\": \"sms.received\"}\n", "events/sms-told.json" to "{}"), tree(device))
        assertEquals(emptyMap<String, String>(), tree(ordinary))
        assertFalse(File(ordinary, "worker").exists())
    }

    @Test fun nothing_to_move_changes_nothing() {
        val (ordinary, device) = storages()
        write(device, "worker/identity.json", "here already")

        Storage.moveFolders(ordinary, device, Storage.FOLDERS)

        assertEquals(mapOf("worker/identity.json" to "here already"), tree(device))
    }

    @Test fun a_move_cut_short_is_done_again_whole() {
        val (ordinary, device) = storages()
        write(ordinary, "worker/identity.json", "the identity")
        write(ordinary, "worker/key", "the key")
        // An earlier move stopped part way: a half copy beside the place, and a part already in it.
        write(device, "worker.moving/identity.json", "the identity")
        write(device, "worker/identity.json", "the identity")

        Storage.moveFolders(ordinary, device, Storage.FOLDERS)

        assertEquals(mapOf("worker/identity.json" to "the identity", "worker/key" to "the key"), tree(device))
        assertFalse(File(device, "worker.moving").exists())
        assertFalse(File(ordinary, "worker").exists())
    }

    @Test fun only_the_folders_named_move() {
        val (ordinary, device) = storages()
        write(ordinary, "worker/key", "k")
        write(ordinary, "something-else/kept.txt", "stays")

        Storage.moveFolders(ordinary, device, listOf("worker"))

        assertEquals(mapOf("worker/key" to "k"), tree(device))
        assertEquals(mapOf("something-else/kept.txt" to "stays"), tree(ordinary))
    }

    @Test fun the_worker_keeps_its_state_its_cards_and_its_events_in_the_storage_read_before_unlock() {
        assertEquals(listOf("worker", "notification-android", "events"), Storage.FOLDERS)
        assertEquals(listOf("rook-worker"), Storage.PREFERENCES)
    }

    @Test fun the_message_before_the_first_unlock_says_why_and_what_unlocks_it() {
        val said = Storage.locked("keeps the contacts closed")
        assertTrue(said, said.startsWith("this phone has not been unlocked since it restarted, and until it is, Android keeps the contacts closed."))
        assertTrue(said, "phone.unlock unlocks it" in said)
    }
}

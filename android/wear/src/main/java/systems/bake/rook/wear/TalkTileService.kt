package systems.bake.rook.wear

import android.content.Context
import androidx.wear.protolayout.ActionBuilders
import androidx.wear.protolayout.ModifiersBuilders
import androidx.wear.protolayout.ResourceBuilders
import androidx.wear.protolayout.TimelineBuilders
import androidx.wear.protolayout.material.Button
import androidx.wear.protolayout.material.ButtonColors
import androidx.wear.protolayout.material.ButtonDefaults
import androidx.wear.protolayout.material.layouts.PrimaryLayout
import androidx.wear.tiles.RequestBuilders
import androidx.wear.tiles.TileBuilders
import androidx.wear.tiles.TileService
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.launch

/**
 * A tile with one button: the Rook, colored by [Talk.Phase] like the talk button. It opens
 * [TalkActivity], which starts listening.
 */
class TalkTileService : TileService() {
    override fun onTileRequest(requestParams: RequestBuilders.TileRequest): ListenableFuture<TileBuilders.Tile> {
        val phase = Talk.state.value.phase
        val open = ActionBuilders.LaunchAction.Builder()
            .setAndroidActivity(ActionBuilders.AndroidActivity.Builder()
                .setPackageName(packageName)
                .setClassName(TalkActivity::class.java.name)
                .build())
            .build()
        val click = ModifiersBuilders.Clickable.Builder().setId("talk").setOnClick(open).build()
        val button = Button.Builder(this, click)
            .setIconContent(ROOK)
            .setButtonColors(ButtonColors(BACKGROUND, phase.color.toInt()))
            .setSize(ButtonDefaults.EXTRA_LARGE_SIZE)
            .setContentDescription(phase.spoken)
            .build()
        val layout = PrimaryLayout.Builder(requestParams.deviceConfiguration)
            .setContent(button)
            .build()
        return Futures.immediateFuture(TileBuilders.Tile.Builder()
            .setResourcesVersion(RESOURCES)
            .setTileTimeline(TimelineBuilders.Timeline.fromLayoutElement(layout))
            .build())
    }

    override fun onTileResourcesRequest(requestParams: RequestBuilders.ResourcesRequest): ListenableFuture<ResourceBuilders.Resources> =
        Futures.immediateFuture(ResourceBuilders.Resources.Builder()
            .setVersion(RESOURCES)
            .addIdToImageMapping(ROOK, ResourceBuilders.ImageResource.Builder()
                .setAndroidResourceByResId(ResourceBuilders.AndroidImageResourceByResId.Builder()
                    .setResourceId(R.drawable.ic_rook_glyph).build())
                .build())
            .build())

    private companion object {
        const val RESOURCES = "2"
        const val ROOK = "rook"
        const val BACKGROUND = 0xFF202124.toInt()
    }
}

/** Redraws the tile whenever the phase changes, for as long as the app's process lives. */
object TalkTile {
    private var following = false

    fun follow(ctx: Context) {
        synchronized(this) { if (following) return; following = true }
        val app = ctx.applicationContext
        CoroutineScope(SupervisorJob() + Dispatchers.Main).launch {
            Talk.state.map { it.phase }.distinctUntilChanged().collect {
                try { TileService.getUpdater(app).requestUpdate(TalkTileService::class.java) } catch (_: Exception) {}
            }
        }
    }
}

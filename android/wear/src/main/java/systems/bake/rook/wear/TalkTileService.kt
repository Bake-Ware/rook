package systems.bake.rook.wear

import androidx.wear.protolayout.ActionBuilders
import androidx.wear.protolayout.ModifiersBuilders
import androidx.wear.protolayout.ResourceBuilders
import androidx.wear.protolayout.TimelineBuilders
import androidx.wear.protolayout.material.Button
import androidx.wear.protolayout.material.ButtonDefaults
import androidx.wear.protolayout.material.layouts.PrimaryLayout
import androidx.wear.tiles.RequestBuilders
import androidx.wear.tiles.TileBuilders
import androidx.wear.tiles.TileService
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture

/** A tile with one big Talk button: it opens [TalkActivity], which starts listening. */
class TalkTileService : TileService() {
    override fun onTileRequest(requestParams: RequestBuilders.TileRequest): ListenableFuture<TileBuilders.Tile> {
        val open = ActionBuilders.LaunchAction.Builder()
            .setAndroidActivity(ActionBuilders.AndroidActivity.Builder()
                .setPackageName(packageName)
                .setClassName(TalkActivity::class.java.name)
                .build())
            .build()
        val click = ModifiersBuilders.Clickable.Builder().setId("talk").setOnClick(open).build()
        val button = Button.Builder(this, click)
            .setTextContent("Talk")
            .setSize(ButtonDefaults.EXTRA_LARGE_SIZE)
            .setContentDescription("Talk to Rook")
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
        Futures.immediateFuture(ResourceBuilders.Resources.Builder().setVersion(RESOURCES).build())

    private companion object { const val RESOURCES = "1" }
}

package systems.bake.rook

import com.google.android.gms.wearable.ChannelClient
import com.google.android.gms.wearable.WearableListenerService
import systems.bake.rook.wearlink.WearLink

/** Wakes when the Rook watch app opens its voice channel; [WatchBridge] runs the conversation. */
class WatchVoiceListener : WearableListenerService() {
    override fun onChannelOpened(channel: ChannelClient.Channel) {
        if (channel.path == WearLink.PATH) WatchBridge.open(this, channel)
    }

    override fun onChannelClosed(channel: ChannelClient.Channel, closeReason: Int, appSpecificErrorCode: Int) {
        if (channel.path == WearLink.PATH) WatchBridge.channelClosed(channel)
    }
}

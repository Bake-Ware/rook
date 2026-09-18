package systems.bake.rook

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Path
import android.transition.AutoTransition
import android.transition.TransitionManager
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.recyclerview.widget.RecyclerView

class ChatMessage(val id: Long, var text: String, val user: Boolean, var key: TurnKey? = null,
                  val image: Bitmap? = null, var alpha: Float = 1f)

class ChatAdapter(private val showThinking: () -> Boolean) : RecyclerView.Adapter<ChatAdapter.Holder>() {
    val messages = mutableListOf<ChatMessage>()
    private val attachments = BubbleDecisions<Long>()
    private var nextId = 0L
    init { setHasStableIds(true) }
    class CornerBubble(ctx: Context) : LinearLayout(ctx) {
        var markerColor: Int? = null
        override fun dispatchDraw(canvas: Canvas) {
            super.dispatchDraw(canvas)
            markerColor?.let { color ->
                val side = 8 * resources.displayMetrics.density
                val right = width.toFloat() - 3 * resources.displayMetrics.density
                val top = 3 * resources.displayMetrics.density
                val path = Path().apply { moveTo(right-side, top); lineTo(right,top); lineTo(right,top+side); close() }
                canvas.drawPath(path, Paint(Paint.ANTI_ALIAS_FLAG).apply { this.color = color })
            }
        }
    }
    class Holder(val row: LinearLayout, val bubble: CornerBubble) : RecyclerView.ViewHolder(row)
    override fun getItemCount() = messages.size
    override fun getItemId(position: Int) = messages[position].id
    fun add(text: String, user: Boolean, image: Bitmap? = null): ChatMessage =
        ChatMessage(++nextId,text,user,image=image).also { messages.add(it); notifyItemInserted(messages.lastIndex) }
    fun changed(m: ChatMessage) { val index=messages.indexOf(m); if(index>=0) notifyItemChanged(index) }
    fun bindTurn(m: ChatMessage, key: TurnKey) { m.key=key; attachments.register(key,m.id,m.user); notifyDataSetChanged() }
    fun decision(key: TurnKey, d: Decision) { attachments.receive(key,d); notifyDataSetChanged() }
    fun expand(key: TurnKey): Int { val id=attachments.expand(key); notifyDataSetChanged(); return messages.indexOfFirst { it.id==id } }
    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Holder {
        val row=LinearLayout(parent.context).apply { orientation=LinearLayout.VERTICAL; setPadding(12,6,12,6)
            layoutParams=RecyclerView.LayoutParams(-1,-2) }
        val bubble=CornerBubble(parent.context).apply { orientation=LinearLayout.VERTICAL }
        row.addView(bubble)
        return Holder(row,bubble)
    }
    override fun onBindViewHolder(holder: Holder, position: Int) = render(holder,messages[position])
    private fun render(holder: Holder, m: ChatMessage) {
        val bubble=holder.bubble; val ctx=bubble.context; val density=ctx.resources.displayMetrics.density
        bubble.removeAllViews(); bubble.alpha=m.alpha
        bubble.setPadding((14*density).toInt(),(10*density).toInt(),(14*density).toInt(),(10*density).toInt())
        bubble.setBackgroundResource(if(m.user) R.drawable.bubble_user else R.drawable.bubble_bot)
        val d=if(showThinking()) attachments.decision(m.key,m.id) else null
        val expanded=d!=null && attachments.isExpanded(m.id)
        val width=(ctx.resources.displayMetrics.widthPixels * if(expanded) 0.9 else 0.8).toInt()
        bubble.layoutParams=LinearLayout.LayoutParams(if(expanded) width else -2,-2).apply { gravity=if(m.user) Gravity.END else Gravity.START }
        m.image?.let { bitmap -> bubble.addView(ImageView(ctx).apply { setImageBitmap(bitmap); adjustViewBounds=true; maxWidth=width-(28*density).toInt() }) }
        if(m.text.isNotEmpty()) bubble.addView(TextView(ctx).apply { text=m.text; textSize=16f; maxWidth=width-(28*density).toInt(); setTextColor(ctx.getColor(R.color.rook_fg)) })
        if(expanded) DecisionContent.fill(bubble,d!!)
        bubble.markerColor=d?.let { ctx.getColor(if(it.engineStatus=="ok") R.color.rook_accent else R.color.rook_dim) }
        bubble.contentDescription=if(d==null) null else "${m.text}. Decision ${d.engineLabel}. Tap to ${if(expanded) "collapse" else "expand"}."
        bubble.setOnClickListener(if(d==null) null else View.OnClickListener {
            TransitionManager.beginDelayedTransition(holder.row, AutoTransition().apply { duration=180 })
            attachments.toggle(m.key,m.id); render(holder,m)
        })
        bubble.isClickable=d!=null; bubble.isFocusable=d!=null; bubble.invalidate()
    }
}

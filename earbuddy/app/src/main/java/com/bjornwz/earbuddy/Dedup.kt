package com.bjornwz.earbuddy

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/**
 * 播报去重：关屏后荣耀会冻结进程，解冻时通知监听重连，
 * 系统会把通知栏里现存的通知按「新通知」重播一遍——不去重就会重复播报。
 * 以 通知key + 发布时间 + 内容指纹 为唯一标识，持久化保存（进程被杀重启也不丢），保留 6 小时。
 */
object Dedup {

    private fun prefs(ctx: Context) =
        ctx.getSharedPreferences("dedup", Context.MODE_PRIVATE)

    fun idOf(pkg: String, key: String, postTime: Long, content: String): String =
        "$pkg|$key|$postTime|${content.hashCode()}"

    @Synchronized
    fun seen(ctx: Context, id: String): Boolean {
        val arr = JSONArray(prefs(ctx).getString("ids", "[]"))
        for (i in 0 until arr.length()) {
            if (arr.getJSONObject(i).getString("id") == id) return true
        }
        return false
    }

    @Synchronized
    fun mark(ctx: Context, id: String) {
        val now = System.currentTimeMillis()
        val old = JSONArray(prefs(ctx).getString("ids", "[]"))
        val arr = JSONArray()
        for (i in 0 until old.length()) {
            val o = old.getJSONObject(i)
            if (now - o.getLong("at") < 6 * 60 * 60 * 1000) arr.put(o)
        }
        arr.put(JSONObject().put("id", id).put("at", now))
        // 上限保护：只留最近 150 条
        val trimmed = JSONArray()
        val from = maxOf(0, arr.length() - 150)
        for (i in from until arr.length()) trimmed.put(arr.get(i))
        prefs(ctx).edit().putString("ids", trimmed.toString()).apply()
    }
}

/**
 * 播报降噪（v0.8.4）：聊天类应用会原地更新同一条通知，未读计数 [N条] 不断变化，
 * 旧的 key+内容指纹去重挡不住——同一条 [图片] 能随计数播上十遍，群聊连发更是逐条全播。
 * 本守卫按「会话」（包名+标题）维护最近播报状态：
 * - 内容指纹先剥离 [N条] 计数：与最近一次播报内容相同（10 分钟内）→ deduped，不播；
 * - 同一会话 60 秒内的后续消息先进暂存槽：槽内同内容只累计计数（coalesced），
 *   异内容到达先把槽内那条立即播出再收新的；槽在安静 20 秒后（最长等 60 秒）只播最新一条。
 * 效果：单条消息照常即时播报，连发合并成一条，不再把旧内容随计数反复念。
 */
object AnnGuard {
    private const val BATCH_WINDOW_MS = 60_000L      // 距上次播报小于此值 → 走暂存槽
    private const val QUIET_MS = 20_000L             // 暂存槽安静多久后播出
    private const val MAX_WAIT_MS = 60_000L          // 暂存槽最长等待
    private const val DEDUP_WINDOW_MS = 10 * 60_000L // 同内容去重窗口

    private class Slot(
        val pkg: String, val speakText: String, val logBody: String,
        val prio: Int, val norm: String, var posts: Int,
        val firstAt: Long, var runnable: Runnable?
    )

    private val handler = android.os.Handler(android.os.Looper.getMainLooper())
    private val lastNorm = mutableMapOf<String, Pair<String, Long>>()
    private val slots = mutableMapOf<String, Slot>()

    private val COUNT_ANY = Regex("\\[\\d+条\\]|【\\d+条】")
    private val COUNT_LEAD = Regex("^(\\[\\d+条\\]|【\\d+条】|\\(\\d+条\\))\\s*")

    /** 剥掉正文开头的未读计数前缀（[13条] 等），播报时不再念计数。 */
    fun stripLeadCount(s: String): String {
        var t = s
        while (true) {
            val n = t.replaceFirst(COUNT_LEAD, "")
            if (n == t) return t
            t = n
        }
    }

    /** 内容指纹：去掉所有计数标记并折叠空白，计数变化不算新内容。 */
    fun normOf(s: String): String =
        s.replace(COUNT_ANY, "").replace(Regex("\\s+"), " ").trim()

    @Synchronized
    fun offer(ctx: Context, pkg: String, convKey: String, speakText: String, logBody: String, prio: Int, norm: String) {
        val appCtx = ctx.applicationContext
        val now = System.currentTimeMillis()
        val slot = slots[convKey]
        // 槽内已有同内容：只累计，不重复播
        if (slot != null && slot.norm == norm) {
            slot.posts++
            DiagLog.log(appCtx, "notify", mapOf("pkg" to pkg, "result" to "coalesced"))
            schedule(appCtx, convKey, slot, now)
            return
        }
        // 与最近一次播报内容相同（只是计数变了）：去重
        val last = lastNorm[convKey]
        if (last != null && last.first == norm && now - last.second < DEDUP_WINDOW_MS) {
            DiagLog.log(appCtx, "notify", mapOf("pkg" to pkg, "result" to "deduped"))
            return
        }
        // 槽里压着一条异内容：先把它播了，再处理当前这条
        if (slot != null) flushSlot(appCtx, convKey)
        val last2 = lastNorm[convKey]
        // 距上次播报不足 60 秒：进暂存槽攒一攒，只播最新一条
        if (last2 != null && System.currentTimeMillis() - last2.second < BATCH_WINDOW_MS) {
            val s = Slot(pkg, speakText, logBody, prio, norm, 1, System.currentTimeMillis(), null)
            slots[convKey] = s
            DiagLog.log(appCtx, "notify", mapOf("pkg" to pkg, "result" to "batched"))
            schedule(appCtx, convKey, s, System.currentTimeMillis())
            return
        }
        announce(appCtx, pkg, convKey, speakText, logBody, prio, norm, 1)
    }

    private fun schedule(ctx: Context, convKey: String, slot: Slot, now: Long) {
        slot.runnable?.let { handler.removeCallbacks(it) }
        val delay = minOf(QUIET_MS, maxOf(0L, slot.firstAt + MAX_WAIT_MS - now))
        val r = Runnable {
            synchronized(AnnGuard) { if (slots[convKey] === slot) flushSlot(ctx, convKey) }
        }
        slot.runnable = r
        handler.postDelayed(r, delay)
    }

    @Synchronized
    private fun flushSlot(ctx: Context, convKey: String) {
        val slot = slots.remove(convKey) ?: return
        slot.runnable?.let { handler.removeCallbacks(it) }
        announce(ctx, slot.pkg, convKey, slot.speakText, slot.logBody, slot.prio, slot.norm, slot.posts)
    }

    private fun announce(ctx: Context, pkg: String, convKey: String, speakText: String, logBody: String, prio: Int, norm: String, posts: Int) {
        Speaker.sayGated(ctx, speakText, prio)
        lastNorm[convKey] = norm to System.currentTimeMillis()
        val (ck, cv) = DiagLog.contentField(ctx, logBody)
        val m = mutableMapOf<String, Any?>("pkg" to pkg, "result" to "announced", "prio" to prio, ck to cv)
        if (posts > 1) m["merged"] = posts
        DiagLog.log(ctx, "notify", m)
    }
}

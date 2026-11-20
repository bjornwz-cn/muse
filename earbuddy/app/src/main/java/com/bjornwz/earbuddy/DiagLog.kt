package com.bjornwz.earbuddy

import android.content.Context
import org.json.JSONObject
import java.io.File
import java.util.concurrent.Executors

/**
 * 诊断日志（2026-11-08 王震令立项）：关键事件落盘成 JSONL，供一键发送/自动上传给 muse 做优化分析。
 * 口径：默认只记元数据（时间、应用、耗时、错误码）；通知正文/识别原文等内容仅在用户打开
 * 「日志含内容」开关后才记。滚动两文件（当前 + 上一份），总量约 1MB 封顶。
 */
object DiagLog {
    private const val PREF = "diag"
    private const val CUR = "diag.jsonl"
    private const val OLD = "diag.1.jsonl"
    private const val MAX_BYTES = 512 * 1024L

    private val io = Executors.newSingleThreadExecutor { r ->
        Thread(r, "diaglog").apply { isDaemon = true }
    }

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE)

    fun includeContent(ctx: Context): Boolean = prefs(ctx).getBoolean("include_content", false)
    fun setIncludeContent(ctx: Context, on: Boolean) =
        prefs(ctx).edit().putBoolean("include_content", on).apply()

    private val SECRET_PATTERNS = listOf(
        Regex("github_pat_[A-Za-z0-9_]{20,}"),
        Regex("gh[pousr]_[A-Za-z0-9]{20,}"),
        Regex("sk-[A-Za-z0-9\\-]{16,}"),
        Regex("(?i)bearer\\s+[A-Za-z0-9._\\-]{16,}")
    )

    /**
     * 密钥打码（v0.8.1）：通知正文可能混入密钥样式串（实例：用户微信里发 GitHub 令牌被播报记录），
     * 不打码会触发 GitHub 推送保护（409 Secret detected）且把密钥扩散进日志仓。记录/导出/上传都过一遍。
     */
    fun scrub(s: String): String {
        var out = s
        for (p in SECRET_PATTERNS) out = p.replace(out, "***")
        return out
    }

    private fun dir(ctx: Context): File = File(ctx.filesDir, "logs").apply { mkdirs() }

    /** 内容字段处理：开关关 → 只留长度；开 → 留原文（截断 300 字）。 */
    fun contentField(ctx: Context, content: String?): Pair<String, Any?> =
        if (content == null) "len" to 0
        else if (includeContent(ctx)) "text" to scrub(content.take(300))
        else "len" to content.length

    fun log(ctx: Context, ev: String, fields: Map<String, Any?> = emptyMap()) {
        val appCtx = ctx.applicationContext
        io.execute { writeLine(appCtx, ev, fields) }
    }

    /** 同步写（崩溃处理里用，异步队列来不及）。 */
    fun logSync(ctx: Context, ev: String, fields: Map<String, Any?> = emptyMap()) {
        try { writeLine(ctx.applicationContext, ev, fields) } catch (e: Exception) { }
    }

    @Synchronized
    private fun writeLine(ctx: Context, ev: String, fields: Map<String, Any?>) {
        try {
            val d = dir(ctx)
            val f = File(d, CUR)
            if (f.exists() && f.length() > MAX_BYTES) {
                val old = File(d, OLD)
                if (old.exists()) old.delete()
                f.renameTo(old)
            }
            val o = JSONObject()
            o.put("ts", System.currentTimeMillis())
            o.put("ev", ev)
            for ((k, v) in fields) o.put(k, v ?: JSONObject.NULL)
            f.appendText(o.toString() + "\n")
        } catch (e: Exception) { /* 日志绝不能拖垮主功能 */ }
    }

    /** 全部日志行（旧文件在前），供导出/上传。文件 ≤1MB，直接读。 */
    fun allLines(ctx: Context): List<String> {
        val d = dir(ctx)
        val out = mutableListOf<String>()
        for (name in listOf(OLD, CUR)) {
            val f = File(d, name)
            if (f.exists()) {
                try { f.bufferedReader().forEachLine { if (it.isNotBlank()) out.add(it) } }
                catch (e: Exception) { }
            }
        }
        return out
    }

    fun stats(ctx: Context): Pair<Long, Int> {
        val lines = allLines(ctx)
        val bytes = lines.sumOf { it.length.toLong() + 1 }
        return bytes to lines.size
    }

    /** 导出为一个文件（一键发送用），放在 cacheDir 经 LogProvider 分享。 */
    fun exportFile(ctx: Context): File {
        val out = File(ctx.cacheDir, "diag-export.jsonl")
        out.writeText(allLines(ctx).map { scrub(it) }.joinToString("\n") + "\n")
        return out
    }
}

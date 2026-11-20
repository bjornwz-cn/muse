package com.bjornwz.earbuddy

import android.content.Context
import android.util.Base64
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * 诊断日志自动上传：把新增日志行 PUT 到用户自己的 GitHub 私有仓（Contents API），
 * muse 侧用 git 拉取后做优化分析。凭据（fine-grained token，仅限该仓 Contents 读写）
 * 由用户在设置页填写，只存本机。水位 = 已成功上传的最大 ts，未成功不推进、可重传。
 */
object DiagUpload {
    private const val PREF = "diag"
    private val io = Executors.newSingleThreadExecutor { r ->
        Thread(r, "diagupload").apply { isDaemon = true }
    }
    private val http = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE)

    fun repo(ctx: Context): String =
        prefs(ctx).getString("up_repo", "bjornwz-cn/earbuddy-logs") ?: "bjornwz-cn/earbuddy-logs"
    fun token(ctx: Context): String = prefs(ctx).getString("up_token", "") ?: ""
    fun autoEnabled(ctx: Context): Boolean = prefs(ctx).getBoolean("up_auto", true)
    fun setConfig(ctx: Context, repo: String, token: String) =
        prefs(ctx).edit().putString("up_repo", repo.trim()).putString("up_token", token.trim()).apply()
    fun setAuto(ctx: Context, on: Boolean) =
        prefs(ctx).edit().putBoolean("up_auto", on).apply()

    private fun lastTs(ctx: Context): Long = prefs(ctx).getLong("up_last_ts", 0)
    private fun lastAttempt(ctx: Context): Long = prefs(ctx).getLong("up_last_attempt", 0)

    /** 保活心跳里调：开了自动上传且距上次尝试 >20 小时才真正尝试（内部节流）。 */
    fun maybeAutoUpload(ctx: Context) {
        val appCtx = ctx.applicationContext
        if (!autoEnabled(appCtx) || token(appCtx).isEmpty()) return
        if (System.currentTimeMillis() - lastAttempt(appCtx) < 20 * 3600 * 1000) return
        uploadNow(appCtx) { _, _ -> }
    }

    fun uploadNow(ctx: Context, cb: (Boolean, String) -> Unit) {
        val appCtx = ctx.applicationContext
        io.execute {
            prefs(appCtx).edit().putLong("up_last_attempt", System.currentTimeMillis()).apply()
            val result = doUpload(appCtx)
            DiagLog.log(appCtx, "upload", mapOf("ok" to result.first, "msg" to result.second))
            cb(result.first, result.second)
        }
    }

    private fun doUpload(ctx: Context): Pair<Boolean, String> {
        val repo = repo(ctx)
        val token = token(ctx)
        if (token.isEmpty()) return false to "未填上传令牌"
        if (!repo.contains('/')) return false to "仓库格式应为 owner/repo"
        val since = lastTs(ctx)
        val fresh = mutableListOf<String>()
        var maxTs = since
        for (line in DiagLog.allLines(ctx)) {
            try {
                val ts = JSONObject(line).optLong("ts", 0)
                if (ts > since) {
                    fresh.add(DiagLog.scrub(line))
                    if (ts > maxTs) maxTs = ts
                }
            } catch (e: Exception) { }
        }
        if (fresh.isEmpty()) return true to "没有新日志"
        val stamp = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())
        val path = "logs/earbuddy-$stamp.jsonl"
        val body = JSONObject()
            .put("message", "diag log $stamp (${fresh.size} lines)")
            .put(
                "content",
                Base64.encodeToString(
                    fresh.joinToString("\n").toByteArray(Charsets.UTF_8),
                    Base64.NO_WRAP
                )
            )
            .toString()
        val req = Request.Builder()
            .url("https://api.github.com/repos/$repo/contents/$path")
            .put(body.toRequestBody("application/json".toMediaType()))
            .header("Authorization", "Bearer $token")
            .header("Accept", "application/vnd.github+json")
            .header("X-GitHub-Api-Version", "2022-11-28")
            .build()
        return try {
            http.newCall(req).execute().use { resp ->
                if (resp.code == 201) {
                    prefs(ctx).edit().putLong("up_last_ts", maxTs).apply()
                    true to "已上传 ${fresh.size} 条"
                } else {
                    val msg = resp.body?.string()?.take(200) ?: ""
                    false to "HTTP ${resp.code} $msg"
                }
            }
        } catch (e: Exception) {
            false to "网络错误：${e.message}"
        }
    }
}

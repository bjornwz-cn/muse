package com.bjornwz.earbuddy

import android.app.Activity
import android.app.AlertDialog
import android.app.ProgressDialog
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

/**
 * OTA 应用内更新：检查托管在 GitHub 仓库的 latest.json，
 * 有新版 → 弹窗说明 → 应用内下载 → 拉起系统安装器（同签名覆盖安装）。
 * 双源（raw 直连 + jsDelivr 镜像）任一通即可，JSON 字段 url/apkUrl、notes/changelog 两种格式都认。
 */
object UpdateManager {
    private val SOURCE_BASES = listOf(
        "https://raw.githubusercontent.com/bjornwz-cn/muse/main/update/",
        "https://cdn.jsdelivr.net/gh/bjornwz-cn/muse@main/update/"
    )

    data class UpdateInfo(
        val versionCode: Int,
        val versionName: String,
        val apkUrls: List<String>,
        val changelog: String
    )

    private fun installedVersionCode(ctx: Context): Int {
        val pi = ctx.packageManager.getPackageInfo(ctx.packageName, 0)
        return if (Build.VERSION.SDK_INT >= 28) pi.longVersionCode.toInt()
        else @Suppress("DEPRECATION") pi.versionCode
    }

    /** 后台检查；回调在主线程。null = 无更新或检查失败（manual=true 时才提示失败/最新）。 */
    fun check(activity: Activity, manual: Boolean) {
        thread {
            val info = fetchLatest()
            DiagLog.log(
                activity, "update_check",
                mapOf(
                    "manual" to manual,
                    "found" to (info?.versionName ?: ""),
                    "hasNew" to (info != null && info.versionCode > installedVersionCode(activity))
                )
            )
            activity.runOnUiThread {
                when {
                    info != null && info.versionCode > installedVersionCode(activity) ->
                        promptDownload(activity, info)
                    manual && info != null ->
                        toast(activity, "已是最新版 ${currentName(activity)}")
                    manual ->
                        toast(activity, "检查更新失败，请稍后再试")
                }
            }
        }
    }

    private fun currentName(ctx: Context): String =
        try { ctx.packageManager.getPackageInfo(ctx.packageName, 0).versionName ?: "" }
        catch (e: Exception) { "" }

    private fun fetchLatest(): UpdateInfo? {
        for (base in SOURCE_BASES) {
            try {
                val conn = URL(base + "latest.json").openConnection() as HttpURLConnection
                conn.connectTimeout = 5000; conn.readTimeout = 5000
                conn.setRequestProperty("Cache-Control", "no-cache")
                val text = conn.inputStream.bufferedReader().use { it.readText() }
                val j = JSONObject(text)
                val jsonUrl = j.optString("apkUrl").ifBlank { j.optString("url") }
                val candidates = LinkedHashSet<String>()
                candidates.add(base + "earbuddy-latest.apk")
                if (jsonUrl.isNotBlank()) candidates.add(jsonUrl)
                SOURCE_BASES.forEach { candidates.add(it + "earbuddy-latest.apk") }
                return UpdateInfo(
                    versionCode = j.getInt("versionCode"),
                    versionName = j.optString("versionName"),
                    apkUrls = candidates.toList(),
                    changelog = j.optString("changelog").ifBlank { j.optString("notes") }
                )
            } catch (e: Exception) {
                // 本源不通 → 试下一个源
            }
        }
        return null
    }

    private fun promptDownload(activity: Activity, info: UpdateInfo) {
        AlertDialog.Builder(activity)
            .setTitle("发现新版 ${info.versionName}")
            .setMessage(info.changelog.ifEmpty { "有可用更新" })
            .setPositiveButton("立即更新") { _, _ ->
                if (Build.VERSION.SDK_INT >= 26 &&
                    !activity.packageManager.canRequestPackageInstalls()
                ) {
                    AlertDialog.Builder(activity)
                        .setMessage("需要先允许「耳机助手」安装应用，点确定去设置里打开开关，回来再点检查更新。")
                        .setPositiveButton("去设置") { _, _ ->
                            activity.startActivity(
                                Intent(
                                    Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                                    Uri.parse("package:${activity.packageName}")
                                )
                            )
                        }
                        .setNegativeButton("取消", null)
                        .show()
                } else {
                    download(activity, info)
                }
            }
            .setNegativeButton("稍后", null)
            .show()
    }

    private fun download(activity: Activity, info: UpdateInfo) {
        val dir = File(activity.cacheDir, "updates").apply { mkdirs() }
        val target = File(dir, "earbuddy-${info.versionName}.apk")
        val dlg = ProgressDialog(activity).apply {
            setMessage("正在下载 ${info.versionName}…")
            setProgressStyle(ProgressDialog.STYLE_HORIZONTAL)
            max = 100
            setCancelable(false)
            show()
        }
        thread {
            var ok = false
            for (url in info.apkUrls) {
                try { target.delete() } catch (e: Exception) { }
                try {
                    val conn = URL(url).openConnection() as HttpURLConnection
                    conn.connectTimeout = 10000; conn.readTimeout = 30000
                    val total = conn.contentLength
                    conn.inputStream.use { input ->
                        target.outputStream().use { out ->
                            val buf = ByteArray(64 * 1024)
                            var done = 0L
                            while (true) {
                                val n = input.read(buf)
                                if (n < 0) break
                                out.write(buf, 0, n); done += n
                                if (total > 0) activity.runOnUiThread {
                                    dlg.progress = (done * 100 / total).toInt()
                                }
                            }
                        }
                    }
                    ok = target.exists() && target.length() > 0 &&
                        (total <= 0 || target.length() == total.toLong())
                    if (ok) break
                } catch (e: Exception) {
                    ok = false // 本源下载失败 → 试下一个源
                }
            }
            activity.runOnUiThread {
                dlg.dismiss()
                if (ok) install(activity, target)
                else toast(activity, "下载失败，请稍后再试")
            }
        }
    }

    private fun install(activity: Activity, apk: File) {
        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(
                ApkProvider.uriFor(apk),
                "application/vnd.android.package-archive"
            )
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        activity.startActivity(intent)
    }

    private fun toast(ctx: Context, msg: String) =
        android.widget.Toast.makeText(ctx, msg, android.widget.Toast.LENGTH_SHORT).show()
}

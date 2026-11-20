package com.bjornwz.earbuddy

import android.app.Activity
import android.content.ComponentName
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.Settings
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast

/** 设置页（软件整体）：初始化步骤 + 语音合成 + 保活自检。播报设备已移至「播报」功能页。 */
class SetupActivity : Activity() {

    private lateinit var healthView: TextView
    private lateinit var diagStatsView: TextView
    private val uiHandler = android.os.Handler(android.os.Looper.getMainLooper())
    private val uiTicker = object : Runnable {
        override fun run() { refreshHealth(); uiHandler.postDelayed(this, 2000) }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.statusBarColor = Ui.ACCENT_DARK
        val root = Ui.pageRoot(this)

        val setup = Ui.card(this)
        setup.addView(Ui.title(this, "初始化（只需做一次）", 18f))
        setup.addView(Ui.button(this, "1 · 授予通知读取权限") {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
        })
        setup.addView(Ui.button(this, "2 · 加入电量白名单（防后台被杀）") { requestBatteryWhitelist() })
        setup.addView(Ui.button(this, "3 · 应用启动管理设为手动（切后台不播报时必做）") { openLaunchManager() })
        setup.addView(Ui.button(this, "4 · 悬浮窗权限（翻译悬浮按钮需要）") { openOverlaySettings() })
        setup.addView(Ui.body(this, "前三步做完，连上蓝牙耳机即可自动播报。另外建议在「最近任务」里下拉锁定本应用，防止一键清理时被杀。", 13f, Ui.SUBTEXT))
        root.addView(setup)

        // ——— 语音合成（播报与译文朗读共用）———
        val tts = Ui.card(this)
        tts.addView(Ui.title(this, "🔊 语音合成", 18f))
        tts.addView(Ui.button(this, "选择语音引擎（降低延迟）") { pickEngine() })
        tts.addView(Ui.button(this, "系统语音合成设置") {
            try { startActivity(Intent("com.android.settings.TTS_SETTINGS")) }
            catch (e: Exception) { Toast.makeText(this, "打开失败，请到系统设置里找「文字转语音」", Toast.LENGTH_SHORT).show() }
        })
        root.addView(tts)

        val health = Ui.card(this)
        health.addView(Ui.title(this, "保活自检（真实检测）", 18f))
        healthView = Ui.body(this, "检测中…", 14f)
        health.addView(healthView)
        health.addView(Ui.button(this, "开始 60 秒后台存活测试") {
            KeepAliveService.startTest(this)
            Toast.makeText(this, "测试开始：请立刻按 Home 键切到其他 App，停留 60 秒以上再回来", Toast.LENGTH_LONG).show()
            refreshHealth()
        })
        health.addView(Ui.body(this, "说明：权限和电量白名单是直读系统状态；后台存活靠服务心跳与上面的实测——进程被冻结/杀死会留下中断记录，测得出就报得出。「最近任务锁定」系统不提供检测接口，只能人工确认。", 12f, Ui.SUBTEXT))
        root.addView(health)

        // ——— 诊断日志（发给 Muse 做优化分析）———
        val diag = Ui.card(this)
        diag.addView(Ui.title(this, "📋 诊断日志", 18f))
        diagStatsView = Ui.body(this, "统计中…", 13f, Ui.SUBTEXT)
        diag.addView(diagStatsView)
        val contentSw = Ui.switchRow(this, "日志包含内容（默认关，仅记元数据）", DiagLog.includeContent(this)) { on ->
            DiagLog.setIncludeContent(this, on)
            Toast.makeText(this, if (on) "已开启：通知/识别原文也会记入日志" else "已关闭：只记元数据", Toast.LENGTH_SHORT).show()
        }
        diag.addView(contentSw.view)
        diag.addView(Ui.button(this, "📤 一键发送日志给 Muse", primary = true) { shareDiagLog() })
        diag.addView(Ui.divider(this))
        diag.addView(Ui.body(this, "自动上传（传到你的 GitHub 私有仓，Muse 定期读取分析）", 13f, Ui.SUBTEXT))
        val repoEdit = android.widget.EditText(this).apply {
            setText(DiagUpload.repo(this@SetupActivity))
            hint = "日志仓库，如 bjornwz-cn/earbuddy-logs"
            textSize = 14f
        }
        diag.addView(repoEdit)
        val tokenEdit = android.widget.EditText(this).apply {
            setText(DiagUpload.token(this@SetupActivity))
            hint = "fine-grained token（仅该仓 Contents 读写）"
            textSize = 14f
            inputType = android.text.InputType.TYPE_CLASS_TEXT or
                android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        diag.addView(tokenEdit)
        val autoSw = Ui.switchRow(this, "每天自动上传", DiagUpload.autoEnabled(this)) { on ->
            DiagUpload.setAuto(this, on)
        }
        diag.addView(autoSw.view)
        diag.addView(Ui.button(this, "保存上传设置") {
            DiagUpload.setConfig(this, repoEdit.text.toString(), tokenEdit.text.toString())
            Toast.makeText(this, "上传设置已保存", Toast.LENGTH_SHORT).show()
        })
        diag.addView(Ui.button(this, "立即上传一次") {
            DiagUpload.setConfig(this, repoEdit.text.toString(), tokenEdit.text.toString())
            DiagUpload.uploadNow(this) { ok, msg ->
                runOnUiThread {
                    Toast.makeText(this, if (ok) "上传成功：$msg" else "上传失败：$msg", Toast.LENGTH_LONG).show()
                    refreshDiag()
                }
            }
        })
        root.addView(diag)

        val sv = ScrollView(this).apply {
            setBackgroundColor(Ui.BG)
            addView(root)
        }
        setContentView(sv)
        Ui.fitSystemBars(sv)
    }

    override fun onResume() {
        super.onResume()
        refreshHealth()
        refreshDiag()
        uiHandler.removeCallbacks(uiTicker)
        uiHandler.postDelayed(uiTicker, 2000)
    }

    private fun refreshDiag() {
        if (!::diagStatsView.isInitialized) return
        val (bytes, lines) = DiagLog.stats(this)
        diagStatsView.text = "当前日志 $lines 条 / 约 ${bytes / 1024} KB（滚动保留约 1MB）" +
            if (DiagUpload.token(this).isEmpty()) "；自动上传未配置令牌" else "；自动上传已配置"
    }

    /** 一键发送：导出日志文件 → 系统分享（用户选 Muse 对话发送）。 */
    private fun shareDiagLog() {
        DiagLog.exportFile(this)
        DiagLog.log(this, "share_log")
        val intent = Intent(Intent.ACTION_SEND).apply {
            type = "application/x-ndjson"
            putExtra(Intent.EXTRA_STREAM, LogProvider.uri())
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        startActivity(Intent.createChooser(intent, "发送诊断日志"))
    }

    override fun onPause() {
        uiHandler.removeCallbacks(uiTicker)
        super.onPause()
    }

    private fun dot(ok: Boolean) = if (ok) "🟢" else "🔴"

    private fun refreshHealth() {
        if (!::healthView.isInitialized) return
        val now = System.currentTimeMillis()
        val hb = KeepAliveService.lastHeartbeat(this)
        val hbAge = if (hb == 0L) -1 else (now - hb) / 1000
        val gapMs = KeepAliveService.lastGapMs(this)
        val gapAt = KeepAliveService.lastGapAt(this)
        val (code, desc) = KeepAliveService.testState(this)
        val testIcon = when (code) { 2 -> "🟢"; 1 -> "🟡"; 0 -> "⚪"; else -> "🔴" }
        healthView.text = buildString {
            appendLine("${dot(SysChecks.isListenerEnabled(this@SetupActivity))} 通知读取权限：${if (SysChecks.isListenerEnabled(this@SetupActivity)) "已授予" else "未授予"}（直读系统）")
            appendLine("${dot(SysChecks.isBatteryWhitelisted(this@SetupActivity))} 电量白名单：${if (SysChecks.isBatteryWhitelisted(this@SetupActivity)) "已加入" else "未加入"}（直读系统）")
            appendLine("${dot(hbAge in 0..25)} 服务心跳：${if (hbAge < 0) "无记录" else if (hbAge <= 25) "正常，${hbAge} 秒前" else "异常，已 ${hbAge} 秒无心跳"}")
            if (gapMs > 60_000 && gapAt > 0) {
                val mins = gapMs / 60000.0
                val whenTxt = java.text.SimpleDateFormat("MM-dd HH:mm", java.util.Locale.CHINA).format(java.util.Date(gapAt))
                appendLine("🟡 后台中断记录：$whenTxt 曾中断约 ${"%.1f".format(mins)} 分钟（冻结/被杀/关机都会留下）")
            }
            appendLine("$testIcon 后台存活测试：$desc")
            val overlayOk = Settings.canDrawOverlays(this@SetupActivity)
            appendLine("${dot(overlayOk)} 悬浮窗权限：${if (overlayOk) "已授予" else "未授予（用悬浮按钮才需要）"}（直读系统）")
            append("⚪ 最近任务锁定：系统无检测接口，请人工确认已锁定")
        }
    }

    private fun requestBatteryWhitelist() {
        if (SysChecks.isBatteryWhitelisted(this)) {
            Toast.makeText(this, "已在白名单中", Toast.LENGTH_SHORT).show(); return
        }
        try {
            startActivity(
                Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                    data = Uri.parse("package:$packageName")
                }
            )
        } catch (e: Exception) {
            startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }
    }

    private fun openOverlaySettings() {
        try {
            startActivity(
                Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName"))
            )
        } catch (e: Exception) {
            Toast.makeText(this, "打开失败：请到系统设置→应用→耳机助手→悬浮窗权限打开", Toast.LENGTH_LONG).show()
        }
    }

    private fun openLaunchManager() {
        val candidates = listOf(
            ComponentName("com.hihonor.systemmanager", "com.hihonor.systemmanager.startupmgr.ui.StartupNormalAppListActivity"),
            ComponentName("com.huawei.systemmanager", "com.huawei.systemmanager.startupmgr.ui.StartupNormalAppListActivity"),
            ComponentName("com.hihonor.systemmanager", "com.hihonor.systemmanager.appcontrol.activity.StartupAppControlActivity")
        )
        for (c in candidates) {
            try { startActivity(Intent().setComponent(c)); return } catch (e: Exception) { }
        }
        try {
            startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")))
            Toast.makeText(this, "请在本页找「启动管理 / 电池」相关项，设为手动并允许后台活动", Toast.LENGTH_LONG).show()
        } catch (e: Exception) {
            Toast.makeText(this, "打开失败：请到系统设置→应用→应用启动管理→耳机助手，关闭自动管理并打开三个开关", Toast.LENGTH_LONG).show()
        }
    }

    private fun pickEngine() {
        var probe: android.speech.tts.TextToSpeech? = null
        probe = android.speech.tts.TextToSpeech(this) { status ->
            if (status == android.speech.tts.TextToSpeech.SUCCESS) {
                val engines = probe?.engines ?: emptyList()
                val current = Speaker.enginePref(this)
                val labels = mutableListOf("跟随系统默认")
                val pkgs = mutableListOf("")
                engines.forEach { labels.add(it.label); pkgs.add(it.name) }
                val checked = pkgs.indexOf(current).coerceAtLeast(0)
                android.app.AlertDialog.Builder(this)
                    .setTitle("选择语音引擎")
                    .setSingleChoiceItems(labels.toTypedArray(), checked) { dlg, which ->
                        Speaker.setEnginePref(this, pkgs[which])
                        Speaker.init(this)
                        Toast.makeText(this, "已切换：${labels[which]}", Toast.LENGTH_SHORT).show()
                        dlg.dismiss()
                    }
                    .setNegativeButton("取消", null)
                    .show()
            } else {
                Toast.makeText(this, "读取引擎列表失败", Toast.LENGTH_SHORT).show()
            }
            probe?.shutdown()
        }
    }
}

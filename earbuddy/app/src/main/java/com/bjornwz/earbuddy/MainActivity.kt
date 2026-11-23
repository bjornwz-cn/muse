package com.bjornwz.earbuddy

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.ViewGroup
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast

/** 主界面（v0.7 三段式）：一、状态（只读总览）；二、功能（播报/翻译，各带卡头快捷开关与功能行）；三、设置（软件整体）。 */
class MainActivity : Activity() {

    private lateinit var masterSwitch: Switch
    private lateinit var stPerm: TextView
    private lateinit var stBatt: TextView
    private lateinit var stBt: TextView
    private lateinit var stTrans: TextView
    private lateinit var stDelay: TextView
    private lateinit var stHealth: TextView
    private lateinit var gateValue: TextView
    private lateinit var dirValue: TextView
    private lateinit var quietValue: TextView
    private val uiHandler = android.os.Handler(android.os.Looper.getMainLooper())
    private val uiTicker = object : Runnable {
        override fun run() { refreshStatus(); uiHandler.postDelayed(this, 3000) }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.statusBarColor = Ui.ACCENT_DARK

        val root = Ui.pageRoot(this)
        val ver = try { packageManager.getPackageInfo(packageName, 0).versionName } catch (e: Exception) { "" }
        root.addView(Ui.title(this, "🎧 耳机助手", 22f))
        root.addView(Ui.body(this, "通知播报 · 实时同传", 13f, Ui.SUBTEXT))

        // ——— 一、状态 ———
        val secStatus = Ui.sectionTitle(this, "状态")
        (secStatus.layoutParams as LinearLayout.LayoutParams).topMargin = Ui.dp(this, 4)
        root.addView(secStatus)
        val statusCard = Ui.card(this)
        fun addStatus(label: String): TextView {
            val r = Ui.statusRow(this, label)
            statusCard.addView(r.view)
            return r.value
        }
        stPerm = addStatus("通知权限")
        statusCard.addView(Ui.divider(this))
        stBatt = addStatus("电量白名单")
        statusCard.addView(Ui.divider(this))
        stBt = addStatus("蓝牙耳机")
        statusCard.addView(Ui.divider(this))
        stTrans = addStatus("实时翻译")
        statusCard.addView(Ui.divider(this))
        stDelay = addStatus("播报延迟")
        statusCard.addView(Ui.divider(this))
        stHealth = addStatus("保活状态")
        root.addView(statusCard)

        // ——— 二、功能 ———
        root.addView(Ui.sectionTitle(this, "功能"))

        // 播报：卡头总开关（快捷开关）+ 规则/耳机/测试
        val notifyCard = Ui.card(this)
        masterSwitch = Ui.styledSwitch(this)
        masterSwitch.setOnCheckedChangeListener { _, checked ->
            Rules.setMasterOn(this, checked)
            refreshStatus()
        }
        notifyCard.addView(Ui.cardHeader(this, "🔔 通知播报", masterSwitch))
        notifyCard.addView(Ui.divider(this))
        val rulesRow = Ui.settingRow(this, "播报规则", "按应用设置") {
            startActivity(Intent(this, AppRulesActivity::class.java))
        }
        notifyCard.addView(rulesRow.view)
        notifyCard.addView(Ui.divider(this))
        val gateRow = Ui.settingRow(this, "播报耳机", "未指定") { pickGateDevice() }
        gateValue = gateRow.value
        notifyCard.addView(gateRow.view)
        notifyCard.addView(Ui.divider(this))
        val testRow = Ui.settingRow(this, "测试播报", "试听一条") {
            Speaker.say(this, "耳机助手，测试播报：微信，张三：今晚八点开会")
            val hint = if (BtGate.isTargetConnected(this)) "已发送测试播报"
                else "已发送测试播报（未连耳机，测试经扬声器；自动播报须连耳机）"
            Toast.makeText(this, hint, Toast.LENGTH_LONG).show()
        }
        notifyCard.addView(testRow.view)
        notifyCard.addView(Ui.divider(this))
        val quietRow = Ui.settingRow(this, "静默时段", "") {
            startActivity(Intent(this, AppRulesActivity::class.java))
        }
        quietValue = quietRow.value
        notifyCard.addView(quietRow.view)
        notifyCard.addView(Ui.divider(this))
        val duckRow = Ui.switchRow(this, "播报时降低媒体音量", Speaker.duckMediaPref(this)) { on ->
            Speaker.setDuckMediaPref(this, on)
            if (!on) Toast.makeText(this, "已关：播报与媒体混音，媒体音量不变", Toast.LENGTH_SHORT).show()
        }
        notifyCard.addView(duckRow.view)
        root.addView(notifyCard)

        // 翻译：卡头悬浮按钮开关（快捷开关）+ 方向/开始/记录
        val transCard = Ui.card(this)
        val overlaySwitch = Ui.styledSwitch(this).apply {
            isChecked = getSharedPreferences("translate", MODE_PRIVATE).getBoolean("overlay_on", false)
            setOnCheckedChangeListener { _, checked ->
                getSharedPreferences("translate", MODE_PRIVATE).edit().putBoolean("overlay_on", checked).apply()
                if (checked) {
                    if (Settings.canDrawOverlays(this@MainActivity)) OverlayService.start(this@MainActivity)
                    else {
                        Toast.makeText(this@MainActivity, "先去下方「设置」开悬浮窗权限", Toast.LENGTH_LONG).show()
                        startActivity(Intent(this@MainActivity, SetupActivity::class.java))
                    }
                } else OverlayService.stop(this@MainActivity)
            }
        }
        transCard.addView(Ui.cardHeader(this, "🎙 实时翻译（悬浮按钮）", overlaySwitch))
        transCard.addView(Ui.divider(this))
        val startRow = Ui.settingRow(this, "开始翻译", "打开即开始") {
            startActivity(Intent(this, TranslateActivity::class.java).putExtra("auto_start", true))
        }
        transCard.addView(startRow.view)
        transCard.addView(Ui.divider(this))
        val dirRow = Ui.settingRow(this, "翻译方向", dirShort()) {
            startActivity(Intent(this, TranslateActivity::class.java))
        }
        dirValue = dirRow.value
        transCard.addView(dirRow.view)
        transCard.addView(Ui.divider(this))
        // v0.9.2：翻译设置直接入口——旧版要进设置只能先点「开始翻译」，
        // 现在不用启动会话就能进翻译页改引擎/方向/密钥。
        val settingsRow = Ui.settingRow(this, "翻译设置", transEngineShort()) {
            startActivity(Intent(this, TranslateActivity::class.java))
        }
        transCard.addView(settingsRow.view)
        transCard.addView(Ui.divider(this))
        val recRow = Ui.settingRow(this, "翻译记录", "") {
            startActivity(Intent(this, RecordsActivity::class.java))
        }
        transCard.addView(recRow.view)
        root.addView(transCard)

        // ——— 三、设置（软件整体）———
        root.addView(Ui.sectionTitle(this, "设置"))
        val settingsCard = Ui.card(this)
        val setupRow = Ui.settingRow(this, "初始化与保活自检", "权限 · 语音 · 保活") {
            startActivity(Intent(this, SetupActivity::class.java))
        }
        settingsCard.addView(setupRow.view)
        settingsCard.addView(Ui.divider(this))
        val updRow = Ui.settingRow(this, "检查更新", "v$ver") {
            UpdateManager.check(this, manual = true)
        }
        settingsCard.addView(updRow.view)
        root.addView(settingsCard)

        val sv = ScrollView(this).apply {
            setBackgroundColor(Ui.BG)
            addView(root)
        }
        setContentView(sv)
        Ui.fitSystemBars(sv)

        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        if (Build.VERSION.SDK_INT >= 31 &&
            checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED
        ) requestPermissions(arrayOf(Manifest.permission.BLUETOOTH_CONNECT), 2)

        masterSwitch.isChecked = Rules.isMasterOn(this)
        KeepAliveService.start(this)
        if (getSharedPreferences("translate", MODE_PRIVATE).getBoolean("overlay_on", false) &&
            Settings.canDrawOverlays(this)
        ) OverlayService.start(this)
        UpdateManager.check(this, manual = false)
    }

    /** 当前翻译方向的短标签（状态/功能区展示用）。 */
    private fun dirShort(): String {
        val f = TranslateStore.dirFrom(this)
        val t = TranslateStore.dirTo(this)
        return when {
            f == "auto" && t == "auto" -> "双向自动"
            f == "auto" -> "自动 → ${TranslateStore.langName(t)}"
            else -> "${TranslateStore.langName(f)} → ${TranslateStore.langName(t)}"
        }
    }

    /** 实际生效的识别引擎短标签（与 TranslateService 同一套判定，所见即所得）。 */
    private fun transEngineShort(): String {
        val eng = TranslateStore.asrEngine(this)
        val f = TranslateStore.dirFrom(this)
        val autoCapable = eng == "volc" && TranslateStore.asrKey(this, "volc").isNotEmpty()
        val eff = if (f == "auto") (if (autoCapable) "auto" else "zh") else f
        val multiLang = eff == "ja" || eff == "ko"
        return when {
            eng == "tx" && TranslateStore.txConfigured(this) -> "腾讯·实时识别"
            eng == "tx" -> "⚠️ 腾讯 Key 未填"
            eng == "volc" && TranslateStore.asrKey(this, "volc").isNotEmpty() ->
                if (multiLang) "火山·一句话识别" else "火山·流式识别"
            eng == "volc" -> "⚠️ 火山 Key 未填"
            CloudAsr.activeChannel(this) != null -> "云端·${CloudAsr.activeChannel(this)!!.name}"
            eng != "vosk" -> "⚠️ Key 未填（离线）"
            else -> "离线识别"
        }
    }

    /** 播报耳机选择：自动（跟随当前连接的耳机）或指定优先设备；只列耳机类，音箱/手表不作播报设备。 */
    private fun pickGateDevice() {
        val devices = BtGate.bondedEarphones(this)
        val labels = mutableListOf("自动选择（跟随当前连接的耳机）")
        devices.forEach { labels.add(it.second) }
        val curMac = BtGate.targetMac(this)
        val curIdx = if (curMac.isEmpty()) 0
            else (devices.indexOfFirst { it.first == curMac } + 1).coerceAtLeast(0)
        android.app.AlertDialog.Builder(this)
            .setTitle("选择播报耳机（自动跟随连接的耳机）")
            .setSingleChoiceItems(labels.toTypedArray(), curIdx) { dlg, which ->
                if (which == 0) BtGate.setTarget(this, "", "")
                else {
                    val (mac, name) = devices[which - 1]
                    BtGate.setTarget(this, mac, name)
                }
                if (::gateValue.isInitialized) gateValue.text = BtGate.modeLabel(this)
                refreshStatus()
                dlg.dismiss()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    override fun onResume() {
        super.onResume()
        if (::masterSwitch.isInitialized) masterSwitch.isChecked = Rules.isMasterOn(this)
        refreshStatus()
        uiHandler.removeCallbacks(uiTicker)
        uiHandler.postDelayed(uiTicker, 3000)
    }

    override fun onPause() {
        uiHandler.removeCallbacks(uiTicker)
        super.onPause()
    }

    private fun dot(ok: Boolean) = if (ok) "🟢" else "🔴"

    private fun refreshStatus() {
        if (!::stPerm.isInitialized) return
        val listenerOn = SysChecks.isListenerEnabled(this)
        val battOk = SysChecks.isBatteryWhitelisted(this)
        val bt = btHeadsetState()
        stPerm.text = "${dot(listenerOn)} ${if (listenerOn) "已授予" else "未授予"}"
        stBatt.text = "${dot(battOk)} ${if (battOk) "已加入" else "未加入"}"
        stBt.text = "${dot(bt.first)} ${bt.second}"
        stTrans.text = if (TranslateStore.isActive(this)) "🟢 翻译中" else "空闲"
        val last = Speaker.lastDelayMs(this)
        val avg = Speaker.avgDelayMs(this)
        stDelay.text = if (last >= 0) "最近 ${last}ms · 平均 ${avg}ms" else "—"
        val hb = KeepAliveService.lastHeartbeat(this)
        val hbAge = if (hb == 0L) -1 else (System.currentTimeMillis() - hb) / 1000
        val hbOk = hbAge in 0..25
        val (code, _) = KeepAliveService.testState(this)
        val testOk = code == 2
        val issues = listOf(!listenerOn, !battOk, !hbOk).count { it }
        stHealth.text = when {
            issues == 0 && testOk -> "🟢 全部正常"
            issues == 0 -> "🟢 正常（存活测试${if (code == 0) "未做" else "待观察"}）"
            else -> "🔴 $issues 项异常"
        }
        if (::gateValue.isInitialized) {
            gateValue.text = BtGate.modeLabel(this)
        }
        if (::dirValue.isInitialized) dirValue.text = dirShort()
        if (::quietValue.isInitialized) {
            val range = "${Rules.fmtMinutes(Rules.quietStart(this))} – ${Rules.fmtMinutes(Rules.quietEnd(this))}"
            quietValue.text = when {
                !Rules.quietEnabled(this) -> "已关闭"
                Rules.inQuietHours(this) -> "$range · 生效中"
                else -> range
            }
        }
    }

    private fun btHeadsetState(): Pair<Boolean, String> = BtGate.describe(this)
}

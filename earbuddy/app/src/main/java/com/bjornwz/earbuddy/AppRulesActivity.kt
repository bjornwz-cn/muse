package com.bjornwz.earbuddy

import android.app.Activity
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.graphics.drawable.Drawable
import android.os.Bundle
import android.view.View
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.EditText
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ListView
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast

/**
 * 播报功能页（内容规划）：
 * ① 播报设备（门闸耳机：选定耳机连上才播报）② 静默时段
 * ③ 应用播报开关（带图标列表）
 */
class AppRulesActivity : Activity() {

    private data class AppRow(val label: String, val pkg: String, val icon: Drawable?)

    private lateinit var gateValue: TextView
    private lateinit var connValue: TextView
    private val uiHandler = android.os.Handler(android.os.Looper.getMainLooper())
    private val uiTicker = object : Runnable {
        override fun run() { refreshGate(); uiHandler.postDelayed(this, 2000) }
    }

    override fun onResume() {
        super.onResume()
        refreshGate()
        uiHandler.removeCallbacks(uiTicker)
        uiHandler.postDelayed(uiTicker, 2000)
    }

    override fun onPause() {
        uiHandler.removeCallbacks(uiTicker)
        super.onPause()
    }

    /** 播报设备状态刷新：连接状态 + 当前指定设备（紧凑行，不再用按钮列表占屏）。 */
    private fun refreshGate() {
        if (!::connValue.isInitialized) return
        if (android.os.Build.VERSION.SDK_INT >= 31 && !BtGate.hasPermission(this)) {
            connValue.text = "🔴 需要蓝牙权限，点「播报耳机」授权"
            gateValue.text = "—"
            return
        }
        BtGate.seed(this)
        val (ok, text) = BtGate.describe(this)
        connValue.text = "${if (ok) "🟢" else "🔴"} $text"
        gateValue.text = BtGate.modeLabel(this)
    }

    /** 播报耳机选择：弹窗单选（任意 / 已配对设备），与主页快捷选择同一套。 */
    private fun pickGateDevice() {
        if (android.os.Build.VERSION.SDK_INT >= 31 && !BtGate.hasPermission(this)) {
            requestPermissions(arrayOf(android.Manifest.permission.BLUETOOTH_CONNECT), 41)
            Toast.makeText(this, "授权后重新点一次「播报耳机」", Toast.LENGTH_SHORT).show()
            return
        }
        val devices = BtGate.bondedEarphones(this)
        val labels = mutableListOf("自动选择（跟随当前连接的耳机）")
        devices.forEach { labels.add(it.second) }
        val curMac = BtGate.targetMac(this)
        val curIdx = if (curMac.isEmpty()) 0
            else devices.indexOfFirst { it.first == curMac } + 1
        android.app.AlertDialog.Builder(this)
            .setTitle("选择播报耳机（自动跟随连接的耳机）")
            .setSingleChoiceItems(labels.toTypedArray(), curIdx.coerceAtLeast(0)) { dlg, which ->
                if (which == 0) BtGate.setTarget(this, "", "")
                else {
                    val (mac, name) = devices[which - 1]
                    BtGate.setTarget(this, mac, name)
                }
                refreshGate()
                dlg.dismiss()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.statusBarColor = Ui.ACCENT_DARK

        val root = Ui.pageRoot(this)

        // ——— ① 播报设备（门闸，紧凑行＋弹窗选择，不占屏）———
        val gate = Ui.card(this)
        gate.addView(Ui.title(this, "🎧 播报设备", 18f))
        val connRow = Ui.statusRow(this, "当前状态")
        connValue = connRow.value
        gate.addView(connRow.view)
        gate.addView(Ui.divider(this))
        val gateRow = Ui.settingRow(this, "播报耳机", "任意蓝牙耳机") { pickGateDevice() }
        gateValue = gateRow.value
        gate.addView(gateRow.view)
        gate.addView(Ui.body(this, "未连播报耳机时通知先暂存，连上后汇总补播；车载/音箱不触发。", 12f, Ui.SUBTEXT))
        root.addView(gate)

        // ——— ② 静默时段（带开关）———
        val cardTop = Ui.card(this)
        val quietSwitch = Ui.styledSwitch(this)
        quietSwitch.isChecked = Rules.quietEnabled(this)
        quietSwitch.setOnCheckedChangeListener { _, isChecked ->
            Rules.setQuietEnabled(this, isChecked)
            Toast.makeText(this, if (isChecked) "静默时段已开启" else "静默时段已关闭", Toast.LENGTH_SHORT).show()
        }
        cardTop.addView(Ui.cardHeader(this, "静默时段", quietSwitch))
        cardTop.addView(Ui.body(this, "时段内不播报（开关关掉后时段不生效）", 13f, Ui.SUBTEXT))
        val quietRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply { bottomMargin = Ui.dp(this@AppRulesActivity, 10) }
        }
        fun timeEdit(value: String) = EditText(this).apply {
            setText(value)
            textSize = 16f
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        val startEdit = timeEdit(Rules.fmtMinutes(Rules.quietStart(this)))
        val endEdit = timeEdit(Rules.fmtMinutes(Rules.quietEnd(this)))
        quietRow.addView(startEdit)
        quietRow.addView(TextView(this).apply {
            text = " → "; textSize = 16f
        })
        quietRow.addView(endEdit)
        cardTop.addView(quietRow)

        cardTop.addView(Ui.button(this, "保存静默时段", primary = true) {
            val s = parseHHmm(startEdit.text.toString())
            val e = parseHHmm(endEdit.text.toString())
            if (s == null || e == null) {
                Toast.makeText(this, "时间格式应为 HH:mm", Toast.LENGTH_SHORT).show()
            } else {
                Rules.setQuiet(this, s, e)
                Toast.makeText(this, "已保存", Toast.LENGTH_SHORT).show()
            }
        })
        root.addView(cardTop)

        val cardApps = Ui.card(this)
        // 应用卡必须在根布局里占满剩余高度，否则会被上面两张卡挤出屏幕（v0.7.0 踩坑：列表看不见）
        cardApps.layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f
        ).apply { bottomMargin = Ui.dp(this@AppRulesActivity, 14) }
        cardApps.addView(Ui.title(this, "应用播报开关", 18f))
        cardApps.addView(Ui.body(this, "关掉的应用，其通知不播报。", 13f, Ui.SUBTEXT))
        val rows = loadApps()
        val list = ListView(this).apply {
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f
            )
            divider = null
            adapter = object : ArrayAdapter<AppRow>(this@AppRulesActivity, 0, rows) {
                override fun getView(pos: Int, convert: View?, parent: ViewGroup): View {
                    val row = getItem(pos)!!
                    val holder: LinearLayout
                    val icon: ImageView
                    val label: TextView
                    val sw: Switch
                    if (convert == null) {
                        holder = LinearLayout(context).apply {
                            orientation = LinearLayout.HORIZONTAL
                            setPadding(0, Ui.dp(context, 10), 0, Ui.dp(context, 10))
                            gravity = android.view.Gravity.CENTER_VERTICAL
                        }
                        icon = ImageView(context).apply {
                            layoutParams = LinearLayout.LayoutParams(
                                Ui.dp(context, 40), Ui.dp(context, 40)
                            ).apply { marginEnd = Ui.dp(context, 12) }
                        }
                        label = TextView(context).apply {
                            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
                        }
                        sw = Ui.styledSwitch(context)
                        holder.addView(icon); holder.addView(label); holder.addView(sw)
                        holder.tag = Triple(icon, label, sw)
                    } else {
                        holder = convert as LinearLayout
                        @Suppress("UNCHECKED_CAST")
                        val t = holder.tag as Triple<ImageView, TextView, Switch>
                        icon = t.first; label = t.second; sw = t.third
                    }
                    icon.setImageDrawable(row.icon)
                    label.text = "${row.label}\n"
                    label.append(android.text.SpannableStringBuilder(row.pkg).apply {
                        setSpan(
                            android.text.style.ForegroundColorSpan(Ui.SUBTEXT),
                            0, length, 0
                        )
                        setSpan(android.text.style.RelativeSizeSpan(0.78f), 0, length, 0)
                    })
                    label.textSize = 15f
                    label.setTextColor(Ui.TEXT)
                    sw.setOnCheckedChangeListener(null)
                    sw.isChecked = Rules.isAppEnabled(context, row.pkg)
                    sw.setOnCheckedChangeListener { _, checked ->
                        Rules.setAppEnabled(context, row.pkg, checked)
                    }
                    return holder
                }
            }
        }
        cardApps.addView(list)
        root.addView(cardApps)
        setContentView(root)
        Ui.fitSystemBars(root)
    }

    private fun loadApps(): List<AppRow> {
        val pm = packageManager
        return pm.getInstalledApplications(PackageManager.GET_META_DATA)
            .filter { it.flags and ApplicationInfo.FLAG_SYSTEM == 0 || it.packageName in ALWAYS_SHOW }
            .map {
                AppRow(
                    pm.getApplicationLabel(it).toString(),
                    it.packageName,
                    try { pm.getApplicationIcon(it) } catch (e: Exception) { null }
                )
            }
            .distinctBy { it.pkg }
            .sortedBy { it.label }
    }

    private fun parseHHmm(s: String): Int? {
        val m = Regex("^(\\d{1,2}):(\\d{2})$").matchEntire(s.trim()) ?: return null
        val h = m.groupValues[1].toInt(); val min = m.groupValues[2].toInt()
        if (h !in 0..23 || min !in 0..59) return null
        return h * 60 + min
    }

    companion object {
        private val ALWAYS_SHOW = setOf(
            "com.tencent.mm", "com.android.mms", "com.hihonor.mms",
            "com.android.dialer", "com.hihonor.dialer", "com.android.incallui"
        )
    }
}

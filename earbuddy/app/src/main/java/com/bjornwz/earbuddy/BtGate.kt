package com.bjornwz.earbuddy

import android.Manifest
import android.bluetooth.BluetoothClass
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.os.Build
import org.json.JSONArray
import org.json.JSONObject

/**
 * 播报门闸：只有耳机类蓝牙设备连接时才自动播报，且自动跟随当前连接的耳机。
 * - 设备按蓝牙设备类分类：耳机/耳麦/眼镜才算播报设备；音箱、车载、手表不触发播报。
 * - 播报设备自动选择：用户可指定一台「优先」设备，它在连就用它；否则自动用最近连接的耳机。
 * - 连接集合由 KeepAliveService 里的 ACL 广播维护，每次判定前再用 AudioManager
 *   当前输出实时对账（reconcile），纠正进程被杀期间漏收的断开事件，防陈旧记录外放。
 * - 门闸关闭期间到达的通知先暂存，耳机连上后汇总补播（通讯类念全文，其余报数）。
 */
object BtGate {
    private const val PREF = "btgate"
    private fun prefs(ctx: Context) = ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE)

    fun hasPermission(ctx: Context): Boolean =
        Build.VERSION.SDK_INT < 31 ||
            ctx.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) ==
            PackageManager.PERMISSION_GRANTED

    // ——— 播报设备（已绑定中选定）———
    fun targetMac(ctx: Context): String = prefs(ctx).getString("target_mac", "") ?: ""
    fun targetName(ctx: Context): String = prefs(ctx).getString("target_name", "") ?: ""
    fun setTarget(ctx: Context, mac: String, name: String) {
        prefs(ctx).edit().putString("target_mac", mac).putString("target_name", name).apply()
    }

    /** 已绑定的音频类设备（耳机/耳麦优先判定：AUDIO_VIDEO 大类或带 A2DP/HFP/HSP 的 UUID）。 */
    fun bondedAudio(ctx: Context): List<Pair<String, String>> {
        if (!hasPermission(ctx)) return emptyList()
        val adapter = (ctx.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)
            ?.adapter ?: return emptyList()
        val out = mutableListOf<Pair<String, String>>()
        try {
            for (d in adapter.bondedDevices ?: emptySet()) {
                val isAudioClass = try {
                    d.bluetoothClass?.majorDeviceClass ==
                        BluetoothClass.Device.Major.AUDIO_VIDEO
                } catch (e: Exception) { false }
                val hasAudioUuid = try {
                    d.uuids?.any {
                        val u = it.toString()
                        u.startsWith("0000110b") || u.startsWith("0000111e") ||
                            u.startsWith("00001108")
                    } ?: false
                } catch (e: Exception) { false }
                if (isAudioClass || hasAudioUuid) {
                    out.add((d.address ?: "") to (d.name ?: "未知设备"))
                }
            }
        } catch (e: Exception) { }
        return out.filter { it.first.isNotEmpty() }
    }

    // ——— 设备分类：只有耳机类可作播报设备，音箱/手表等不触发播报 ———
    enum class DevKind { EARPHONE, SPEAKER, OTHER }

    private fun bondedDevice(ctx: Context, mac: String): BluetoothDevice? {
        if (!hasPermission(ctx)) return null
        val adapter = (ctx.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)
            ?.adapter ?: return null
        return try { adapter.bondedDevices?.firstOrNull { it.address == mac } }
        catch (e: Exception) { null }
    }

    /** 按蓝牙设备类判定：耳机/耳麦/眼镜→EARPHONE，音箱/车载音响→SPEAKER，手表及其他→OTHER。 */
    fun kindOf(ctx: Context, mac: String): DevKind {
        val dev = bondedDevice(ctx, mac)
        val cls = try { dev?.bluetoothClass?.deviceClass ?: -1 } catch (e: Exception) { -1 }
        when (cls) {
            BluetoothClass.Device.AUDIO_VIDEO_WEARABLE_HEADSET,
            BluetoothClass.Device.AUDIO_VIDEO_HANDSFREE,
            BluetoothClass.Device.AUDIO_VIDEO_HEADPHONES -> return DevKind.EARPHONE
            BluetoothClass.Device.AUDIO_VIDEO_LOUDSPEAKER,
            BluetoothClass.Device.AUDIO_VIDEO_HIFI_AUDIO,
            BluetoothClass.Device.AUDIO_VIDEO_PORTABLE_AUDIO,
            BluetoothClass.Device.AUDIO_VIDEO_CAR_AUDIO -> return DevKind.SPEAKER
        }
        // 设备类拿不到或未细分时的兜底：名字像音箱/手表先排除，再看协议
        val uuids = try { dev?.uuids?.map { it.toString() } ?: emptyList() }
            catch (e: Exception) { emptyList() }
        val name = (try { dev?.name } catch (e: Exception) { null } ?: "").lowercase()
        if (name.contains("音箱") || name.contains("speaker") ||
            name.contains("watch") || name.contains("手表")) return DevKind.OTHER
        if (uuids.any { it.startsWith("0000111e") || it.startsWith("00001108") ||
                it.startsWith("0000110b") }) return DevKind.EARPHONE
        return DevKind.OTHER
    }

    /** 已配对的耳机类设备（音箱/手表不列入播报候选）。 */
    fun bondedEarphones(ctx: Context): List<Pair<String, String>> =
        bondedAudio(ctx).filter { kindOf(ctx, it.first) == DevKind.EARPHONE }

    // ——— 当前连接集合（mac）———
    private fun connSet(ctx: Context): MutableSet<String> =
        (prefs(ctx).getStringSet("conn_macs", emptySet()) ?: emptySet()).toMutableSet()

    private fun saveConn(ctx: Context, set: Set<String>) {
        prefs(ctx).edit().putStringSet("conn_macs", set).apply()
    }

    fun onAcl(ctx: Context, mac: String?, connected: Boolean) {
        if (mac.isNullOrEmpty()) return
        val audioMacs = bondedAudio(ctx).map { it.first }.toSet()
        if (audioMacs.isNotEmpty() && mac !in audioMacs) return  // 非音频设备不记
        val s = connSet(ctx)
        if (connected) s.add(mac) else s.remove(mac)
        saveConn(ctx, s)
        // 连接顺序（最近连接的排最后）：多台耳机同时连接时自动选最近连上的
        val order = connOrder(ctx)
        order.remove(mac)
        if (connected) order.add(mac)
        saveOrder(ctx, order)
    }

    /** 连接顺序表（mac 列表，最近连接的在最后）。 */
    private fun connOrder(ctx: Context): MutableList<String> {
        val arr = JSONArray(prefs(ctx).getString("conn_order", "[]"))
        val out = mutableListOf<String>()
        for (i in 0 until arr.length()) out.add(arr.getString(i))
        return out
    }

    private fun saveOrder(ctx: Context, list: List<String>) {
        val arr = JSONArray()
        list.forEach { arr.put(it) }
        prefs(ctx).edit().putString("conn_order", arr.toString()).apply()
    }

    /** 清空连接集合（蓝牙关闭时调用）。 */
    fun clearConnections(ctx: Context) {
        saveConn(ctx, emptySet())
        saveOrder(ctx, emptyList())
        prefs(ctx).edit().putBoolean("any_bt_out", false).apply()
    }

    /**
     * 实时对账：连接集合只靠 ACL 广播增删，进程被杀期间耳机断开（放回仓、关蓝牙）时
     * 断开广播无人接收，集合里会留下陈旧记录，门闸据此误判「耳机在连」而外放。
     * 每次判定前先用系统当前状态纠正：
     * - 蓝牙已关 / 当前无任何蓝牙音频输出 → 陈旧集合一律清空；
     * - 有带地址的蓝牙输出 → 连接集合以实时集合为准重写；
     * - 有蓝牙输出但拿不到地址（老设备）→ 只刷新 any_bt_out，集合不动，走旧兜底。
     */
    fun reconcile(ctx: Context) {
        val adapterOff = try {
            val a = (ctx.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter
            a != null && !a.isEnabled
        } catch (e: Exception) { false }
        if (adapterOff) { clearConnections(ctx); return }
        val am = ctx.getSystemService(Context.AUDIO_SERVICE) as? AudioManager ?: return
        var anyBt = false
        val live = mutableSetOf<String>()
        try {
            for (d in am.getDevices(AudioManager.GET_DEVICES_OUTPUTS)) {
                if (d.type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP ||
                    d.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO ||
                    d.type == AudioDeviceInfo.TYPE_BLE_HEADSET ||
                    d.type == AudioDeviceInfo.TYPE_BLE_BROADCAST
                ) {
                    anyBt = true
                    if (Build.VERSION.SDK_INT >= 28) {
                        val addr = try { d.address } catch (e: Exception) { "" }
                        if (!addr.isNullOrEmpty() && addr != "00:00:00:00:00:00") live.add(addr)
                    }
                }
            }
        } catch (e: Exception) { return }
        prefs(ctx).edit().putBoolean("any_bt_out", anyBt).apply()
        if (!anyBt) {
            if (connSet(ctx).isNotEmpty()) {
                saveConn(ctx, emptySet())
                saveOrder(ctx, emptyList())
                DiagLog.log(ctx, "gate_reconcile", mapOf("cleared" to "stale"))
            }
            return
        }
        if (live.isNotEmpty() && connSet(ctx) != live) {
            val kept = connOrder(ctx).filter { it in live }
            saveConn(ctx, live)
            saveOrder(ctx, kept + (live - kept.toSet()).sorted())
            DiagLog.log(ctx, "gate_reconcile", mapOf("live" to live.size))
        }
    }

    /** 兼容旧调用点：种子与对账已统一为实时对账。 */
    fun seed(ctx: Context) = reconcile(ctx)

    /** 当前已连接的耳机类设备（按连接顺序，最近连接的排最后）。判定前先实时对账。 */
    fun connectedEarphones(ctx: Context): List<Pair<String, String>> {
        reconcile(ctx)
        val set = connSet(ctx)
        val ears = bondedAudio(ctx).filter {
            it.first in set && kindOf(ctx, it.first) == DevKind.EARPHONE
        }
        val order = connOrder(ctx)
        return ears.sortedBy { order.indexOf(it.first) }
    }

    /**
     * 当前生效的播报设备（自动跟随）：用户指定的设备在连就优先用它；
     * 否则自动用最近连接的那台耳机。没有耳机连接返回 null（不播报）。
     */
    fun activeTarget(ctx: Context): Pair<String, String>? {
        val ears = connectedEarphones(ctx)
        if (ears.isEmpty()) return null
        val pref = targetMac(ctx)
        ears.firstOrNull { it.first == pref }?.let { return it }
        return ears.last()
    }

    /** 播报耳机行的模式文案：自动选择 / 优先某台。 */
    fun modeLabel(ctx: Context): String {
        val pref = targetMac(ctx)
        return if (pref.isEmpty()) "自动选择（跟随连接的耳机）"
        else "优先：${targetName(ctx)}"
    }

    /** 门闸判定：先实时对账，再看有无耳机类设备连接（自动跟随），音箱/手表不算。 */
    fun isTargetConnected(ctx: Context): Boolean {
        reconcile(ctx)
        if (activeTarget(ctx) != null) return true
        // 集合非空但没有耳机类（只连了音箱/手表）→ 不放行；
        // 集合为空但系统确有蓝牙音频输出且拿不到地址（老设备）→ 退回兜底放行
        if (connSet(ctx).isNotEmpty()) return false
        return prefs(ctx).getBoolean("any_bt_out", false)
    }

    /** 状态文案（主界面用）：ok=可播报，text=说明。 */
    fun describe(ctx: Context): Pair<Boolean, String> {
        val ears = connectedEarphones(ctx)
        val active = activeTarget(ctx)
        val pref = targetMac(ctx)
        if (active != null) {
            return when {
                ears.size > 1 -> true to "${active.second} 已连接（共 ${ears.size} 台耳机，自动用它）"
                pref.isNotEmpty() && active.first == pref ->
                    true to "${active.second} 已连接（播报设备）"
                else -> true to "${active.second} 已连接（自动播报设备）"
            }
        }
        var set = connSet(ctx)
        if (set.isEmpty()) { seed(ctx); set = connSet(ctx) }
        if (set.isEmpty() && prefs(ctx).getBoolean("any_bt_out", false)) {
            return true to "蓝牙音频已连接"
        }
        val others = bondedAudio(ctx).filter { it.first in set }
        return if (others.isNotEmpty()) {
            false to "已连接 ${others.first().second}（非耳机，不播报）"
        } else {
            false to "未连接耳机（不播报）"
        }
    }

    // ——— 门闸关闭期间的暂存与补播 ———
    fun hold(ctx: Context, appName: String, body: String, important: Boolean) {
        val arr = JSONArray(prefs(ctx).getString("hold", "[]"))
        arr.put(
            JSONObject().put("app", appName).put("body", body)
                .put("imp", important).put("t", System.currentTimeMillis())
        )
        var droppedNow = 0
        while (arr.length() > 20) { arr.remove(0); droppedNow++ }
        val e = prefs(ctx).edit().putString("hold", arr.toString())
        if (droppedNow > 0) {
            e.putInt("hold_dropped", prefs(ctx).getInt("hold_dropped", 0) + droppedNow)
        }
        e.apply()
    }

    /** 整段文本暂存（播出前验闸未通过时回存；app 名为空，补播时只念正文）。 */
    fun holdText(ctx: Context, text: String, important: Boolean) {
        if (text.isBlank()) return
        hold(ctx, "", text, important)
    }

    /** 耳机连上后调用：有暂存就汇总补播并清空。 */
    fun flush(ctx: Context) {
        val raw = prefs(ctx).getString("hold", "[]") ?: "[]"
        val arr = JSONArray(raw)
        if (arr.length() == 0) return
        val dropped = prefs(ctx).getInt("hold_dropped", 0)
        prefs(ctx).edit().remove("hold").remove("hold_dropped").apply()
        val now = System.currentTimeMillis()
        val items = mutableListOf<Triple<String, String, Boolean>>()
        var expired = 0
        for (i in 0 until arr.length()) {
            val o = arr.getJSONObject(i)
            if (now - o.optLong("t", now) > 3 * 60 * 60 * 1000) { expired++; continue }  // 超 3 小时不补
            items.add(Triple(o.optString("app"), o.optString("body"), o.optBoolean("imp")))
        }
        if (items.isEmpty()) return
        DiagLog.log(ctx, "gate_flush", mapOf(
            "count" to items.size, "important" to items.count { it.third },
            "expired" to expired, "dropped" to dropped
        ))
        fun line(app: String, body: String) = if (app.isEmpty()) "$body。" else "$app，$body。"
        val sb = StringBuilder("耳机已连接。未连接期间共有 ${items.size} 条通知。")
        val imp = items.filter { it.third }
        val rest = items.filter { !it.third }
        for ((app, body, _) in imp) sb.append(line(app, body))
        if (imp.isEmpty() && rest.isNotEmpty()) {
            sb.append("其中：")
            for ((app, body, _) in rest.take(3)) sb.append(line(app, body))
            if (rest.size > 3) sb.append("其余 ${rest.size - 3} 条略。")
        } else if (rest.isNotEmpty()) {
            val apps = rest.map { it.first }.filter { it.isNotEmpty() }.distinct().take(4).joinToString("、")
            sb.append(if (apps.isEmpty()) "另有 ${rest.size} 条通知。" else "另有 ${rest.size} 条来自 $apps 等。")
        }
        Speaker.sayGated(ctx, sb.toString(), 1)
    }
}

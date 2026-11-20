package com.bjornwz.earbuddy

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.media.ToneGenerator
import android.os.Handler
import android.os.Looper
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.util.Log
import java.util.Locale

/**
 * TTS 播报器：串行队列，一条播完再播下一条。
 * 播报时是否压低媒体音量由开关控制（默认关）：
 * - 关：播报与媒体混音同步出声，不申请音频焦点，媒体音量全程不受影响；
 * - 开：播报前申请 MAY_DUCK 焦点压低媒体音，播完释放，并全程记日志
 *   （focus_request / focus_abandon / tts_start / tts_done / tts_error /
 *   tts_speak_fail / focus_watchdog），另有看门狗兜底：播报超时未收到完成
 *   回调即强制释放焦点、复位状态，防止媒体音量被压住回不来（v0.8.7 旧
 *   焦点泄漏的根因，见 v0.8.8 说明）。
 * 音频走 USAGE_MEDIA，蓝牙 A2DP 连接时系统自动路由到耳机。
 */
object Speaker {
    private const val TAG = "Speaker"
    private var tts: TextToSpeech? = null
    private var ready = false

    // ——— 延迟度量：say() 时刻 → TTS 真正出声（onStart）的间隔 ———
    @Volatile private var sayStartedAt = 0L
    @Volatile private var lastWarmAt = 0L

    private fun statsPrefs(ctx: Context) =
        ctx.getSharedPreferences("stats", Context.MODE_PRIVATE)

    /** 最近一次播报延迟（ms），无数据为 -1。 */
    fun lastDelayMs(ctx: Context): Long = statsPrefs(ctx).getLong("last_delay", -1)

    /** 近 10 次平均延迟（ms），无数据为 -1。 */
    fun avgDelayMs(ctx: Context): Long {
        val raw = statsPrefs(ctx).getString("delay_ring", "") ?: ""
        val vals = raw.split(',').mapNotNull { it.toLongOrNull() }
        return if (vals.isEmpty()) -1 else vals.sum() / vals.size
    }

    private fun recordDelay(ctx: Context, ms: Long) {
        val p = statsPrefs(ctx)
        val raw = p.getString("delay_ring", "") ?: ""
        val vals = (raw.split(',').mapNotNull { it.toLongOrNull() } + ms).takeLast(10)
        p.edit().putLong("last_delay", ms).putString("delay_ring", vals.joinToString(",")).apply()
    }

    /** 用户选定的 TTS 引擎包名（空 = 跟随系统默认）。 */
    fun enginePref(ctx: Context): String =
        ctx.getSharedPreferences("speaker", Context.MODE_PRIVATE).getString("tts_engine", "") ?: ""

    fun setEnginePref(ctx: Context, pkg: String) {
        ctx.getSharedPreferences("speaker", Context.MODE_PRIVATE)
            .edit().putString("tts_engine", pkg).apply()
        shutdown()
    }

    /** 播报时是否压低媒体音量（默认关 = 混音播报，不申请音频焦点）。 */
    fun duckMediaPref(ctx: Context): Boolean =
        ctx.getSharedPreferences("speaker", Context.MODE_PRIVATE).getBoolean("duck_media", false)

    fun setDuckMediaPref(ctx: Context, on: Boolean) {
        ctx.getSharedPreferences("speaker", Context.MODE_PRIVATE)
            .edit().putBoolean("duck_media", on).apply()
        DiagLog.log(ctx.applicationContext, "duck_pref", mapOf("on" to on))
        if (!on) appCtxRef?.let { abandonFocus(it, "duck_off") }
    }

    // ——— 音频焦点状态（仅 duck 开时存在；全程记日志 + 看门狗兜底）———
    private var focusReq: AudioFocusRequest? = null
    @Volatile private var focusHeld = false
    @Volatile private var currentUtteranceId: String? = null
    @Volatile private var currentUtteranceStartedAt = 0L
    private val mainHandler = Handler(Looper.getMainLooper())
    private var watchdogRunnable: Runnable? = null
    private var appCtxRef: Context? = null

    @Synchronized
    fun shutdown() {
        cancelWatchdog()
        currentUtteranceId = null
        appCtxRef?.let { abandonFocus(it, "shutdown") }
        try { tts?.shutdown() } catch (e: Exception) {}
        tts = null
        ready = false
        speaking = false
    }

    /** 待播项：优先级（0 最高：通讯类，1 其他）+ 入队序号，同级先到先播。
     *  gated=true 的是通知播报：出声前逐条验闸，耳机已断开绝不外放。 */
    private data class Item(val priority: Int, val seq: Long, val text: String, val gated: Boolean = false)
    private val pending = mutableListOf<Item>()
    private val deferred = mutableListOf<Item>()
    @Volatile private var lastText = ""
    @Volatile private var lastTextAt = 0L
    @Volatile private var speaking = false

    /** TTS 是否正在朗读（内部音频采集据此丢帧防回授）。 */
    fun isSpeakingNow(): Boolean = speaking
    private var seq = 0L

    @Synchronized
    fun init(ctx: Context) {
        if (tts != null) return
        val appCtx = ctx.applicationContext
        appCtxRef = appCtx
        val enginePkg = enginePref(appCtx)
        val listener = TextToSpeech.OnInitListener { status ->
            if (status == TextToSpeech.SUCCESS) {
                tts?.language = Locale.CHINESE
                tts?.setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build()
                )
                tts?.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                    override fun onStart(id: String?) {
                        speaking = true
                        if (sayStartedAt > 0) {
                            val delay = System.currentTimeMillis() - sayStartedAt
                            recordDelay(appCtx, delay)
                            DiagLog.log(appCtx, "tts_start", mapOf("delayMs" to delay, "id" to id, "duck" to duckMediaPref(appCtx)))
                            sayStartedAt = 0
                        }
                    }
                    override fun onDone(id: String?) {
                        val started = currentUtteranceStartedAt
                        val dur = if (started > 0) System.currentTimeMillis() - started else -1
                        DiagLog.log(appCtx, "tts_done", mapOf("id" to id, "durMs" to dur))
                        cancelWatchdog()
                        currentUtteranceId = null
                        speaking = false; drain(appCtx)
                    }
                    @Deprecated("legacy") override fun onError(id: String?) {
                        DiagLog.log(appCtx, "tts_error", mapOf("id" to id))
                        cancelWatchdog()
                        currentUtteranceId = null
                        speaking = false; drain(appCtx)
                    }
                    override fun onError(id: String?, code: Int) {
                        DiagLog.log(appCtx, "tts_error", mapOf("id" to id, "code" to code))
                        cancelWatchdog()
                        currentUtteranceId = null
                        speaking = false; drain(appCtx)
                    }
                })
                ready = true
                warmNow(appCtx)
                drain(appCtx)
            } else {
                Log.e(TAG, "TTS init failed: $status")
            }
        }
        tts = if (enginePkg.isNotEmpty()) TextToSpeech(appCtx, listener, enginePkg)
        else TextToSpeech(appCtx, listener)
    }

    /** 保温：静音合成一句，让引擎保持热启动状态，压低开口延迟。 */
    private fun warmNow(ctx: Context) {
        val engine = tts
        if (!ready || engine == null) return
        lastWarmAt = System.currentTimeMillis()
        try {
            val params = android.os.Bundle().apply {
                putFloat(TextToSpeech.Engine.KEY_PARAM_VOLUME, 0.0f)
            }
            engine.speak("。", TextToSpeech.QUEUE_ADD, params, "warm")
        } catch (e: Exception) { Log.w(TAG, "warm failed: $e") }
    }

    /** 常驻服务每几秒调一次：空闲且距上次保温 >4 分钟就再保温。 */
    fun keepWarm(ctx: Context) {
        val appCtx = ctx.applicationContext
        init(appCtx)
        val idle = synchronized(pending) { pending.isEmpty() } && !speaking
        if (idle && System.currentTimeMillis() - lastWarmAt > 4 * 60 * 1000) warmNow(appCtx)
    }

    /** 入队一条播报文本，自动清洗与截断。通话中只提示不播报：存入待播队列并轻响一声，通话结束补播。
     *  多条同时到达时按 priority 排序逐条播报（同级按到达先后），正在播的那条不打断。
     *  翻译朗读、手动测试走这里（不受门闸约束，去向由各自设置决定）。 */
    fun say(ctx: Context, raw: String, priority: Int = 2) = enqueue(ctx, raw, priority, false)

    /** 通知播报专用入口：条目带门闸标记，出声前逐条验闸（见 drain）。 */
    fun sayGated(ctx: Context, raw: String, priority: Int = 2) = enqueue(ctx, raw, priority, true)

    private fun enqueue(ctx: Context, raw: String, priority: Int, gated: Boolean) {
        val text = raw.replace(Regex("\\s+"), " ").trim().take(220)
        if (text.isEmpty()) return
        val appCtx = ctx.applicationContext
        synchronized(pending) {
            // 队列层兜底去重：同一文本 30 秒内重复入队直接丢弃（防系统重播穿透）
            val now = System.currentTimeMillis()
            if (text == lastText && now - lastTextAt < 30_000) return
            if (pending.any { it.text == text } || deferred.any { it.text == text }) return
            lastText = text; lastTextAt = now
            val item = Item(priority, seq++, text, gated)
            if (inCall(appCtx)) {
                deferred.add(item)
                beep(appCtx)
                return
            }
            if (pending.isEmpty() && !speaking) sayStartedAt = System.currentTimeMillis()
            pending.add(item)
        }
        init(appCtx)
        drain(appCtx)
    }

    // ——— 通话状态处理 ———
    @Volatile private var lastBeepAt = 0L

    /** 正在通话（含微信等网络通话，AudioManager 模式判定，无需额外权限）。 */
    fun inCall(ctx: Context): Boolean {
        val am = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        return am.mode == AudioManager.MODE_IN_CALL || am.mode == AudioManager.MODE_IN_COMMUNICATION
    }

    /** 仅传统电话通话（不含 VoIP/会议软件）：翻译会话据此暂停——会议软件走 IN_COMMUNICATION，
     * 若也暂停，腾讯会议等场景的同传会全程被误停。 */
    fun inVoiceCall(ctx: Context): Boolean {
        val am = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        return am.mode == AudioManager.MODE_IN_CALL
    }

    /** 当前是否有蓝牙音频输出在连（A2DP/SCO/BLE 耳机类，与 BtGate 的判定口径一致）。 */
    fun btAudioConnected(ctx: Context): Boolean {
        val am = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        return try {
            am.getDevices(AudioManager.GET_DEVICES_OUTPUTS).any {
                it.type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP ||
                    it.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO ||
                    it.type == AudioDeviceInfo.TYPE_BLE_HEADSET ||
                    it.type == AudioDeviceInfo.TYPE_BLE_BROADCAST
            }
        } catch (e: Exception) { false }
    }

    /** 通话中的轻提示音（走通话音频通道，只有本人能听到；3 秒内不重复响）。 */
    private fun beep(ctx: Context) {
        val now = System.currentTimeMillis()
        if (now - lastBeepAt < 3000) return
        lastBeepAt = now
        try {
            val tg = ToneGenerator(AudioManager.STREAM_VOICE_CALL, 45)
            tg.startTone(ToneGenerator.TONE_PROP_BEEP2, 220)
            Handler(Looper.getMainLooper()).postDelayed({ try { tg.release() } catch (e: Exception) {} }, 500)
        } catch (e: Exception) {
            Log.w(TAG, "beep failed: $e")
        }
    }

    /** 通话结束后补播积压的通知（由常驻服务定时调用），同样按优先级排序。 */
    fun flushDeferred(ctx: Context) {
        val appCtx = ctx.applicationContext
        if (inCall(appCtx)) return
        synchronized(pending) {
            if (deferred.isEmpty()) return
            val items = deferred.sortedWith(compareBy({ it.priority }, { it.seq }))
            deferred.clear()
            if (items.size > 1) pending.add(Item(-1, seq++, "通话期间有 ${items.size} 条通知", items.any { it.gated }))
            pending.addAll(items)
        }
        init(appCtx)
        drain(appCtx)
    }

    @Synchronized
    private fun drain(ctx: Context) {
        val engine = tts
        if (!ready || engine == null || speaking) return
        while (true) {
            // 取优先级最高（数字最小）的一条，同级取最早入队的
            val next = synchronized(pending) {
                val best = pending.minWithOrNull(compareBy({ it.priority }, { it.seq }))
                if (best != null) pending.remove(best)
                best
            } ?: run {
                abandonFocus(ctx, "queue_empty")
                return
            }
            // 通知播报出声前验闸：排队/暂存期间耳机可能已断开，此时绝不外放，
            // 回存暂存区，等耳机重连后汇总补播；然后继续处理下一条
            if (next.gated && !BtGate.isTargetConnected(ctx)) {
                DiagLog.log(ctx, "gate_blocked_at_speak", mapOf("prio" to next.priority))
                BtGate.holdText(ctx, next.text, next.priority == 0)
                continue
            }
            val id = "u${seq++}"
            currentUtteranceId = id
            currentUtteranceStartedAt = System.currentTimeMillis()
            // 开关关（默认）：不申请焦点，与媒体混音同步出声；
            // 开关开：申请 MAY_DUCK 焦点压低媒体音，全程记日志 + 看门狗兜底
            requestFocus(ctx, id)
            speaking = true
            val r = engine.speak(next.text, TextToSpeech.QUEUE_ADD, null, id)
            if (r != TextToSpeech.SUCCESS) {
                DiagLog.log(ctx, "tts_speak_fail", mapOf("id" to id, "result" to r))
                cancelWatchdog()
                currentUtteranceId = null
                speaking = false
                continue
            }
            armWatchdog(ctx, id, next.text.length)
            return
        }
    }

    fun stop() {
        synchronized(pending) { pending.clear(); deferred.clear() }
        cancelWatchdog()
        currentUtteranceId = null
        tts?.stop()
        speaking = false
        appCtxRef?.let { abandonFocus(it, "stop") }
    }

    // ——— 音频焦点（仅 duck 开时申请；全程记日志 + 看门狗兜底）———
    private fun requestFocus(ctx: Context, utteranceId: String) {
        if (!duckMediaPref(ctx)) return
        // 同一时间只持有一份焦点：先释放旧的再申请新的
        focusReq?.let { abandonFocus(ctx, "replace") }
        val am = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val attrs = AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_MEDIA)
            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build()
        val req = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
            .setAudioAttributes(attrs).build()
        val res = am.requestAudioFocus(req)
        focusReq = req
        focusHeld = res == AudioManager.AUDIOFOCUS_REQUEST_GRANTED
        DiagLog.log(ctx, "focus_request", mapOf("id" to utteranceId, "result" to res, "granted" to focusHeld))
    }

    private fun abandonFocus(ctx: Context, reason: String) {
        val req = focusReq ?: return
        val am = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val res = am.abandonAudioFocusRequest(req)
        DiagLog.log(ctx, "focus_abandon", mapOf("reason" to reason, "result" to res))
        focusReq = null
        focusHeld = false
    }

    /** 看门狗：播报超时仍未收到完成回调（onDone/onError 丢失），强制释放焦点、
     *  复位状态并继续下一条，防止媒体音量被压住回不来。语速按每字约 300ms
     *  估算，另加 8 秒余量，上限 90 秒。 */
    private fun armWatchdog(ctx: Context, utteranceId: String, textLen: Int) {
        if (!duckMediaPref(ctx)) return
        cancelWatchdog()
        val delayMs = (textLen * 300L + 8000L).coerceAtMost(90_000L)
        val r = Runnable {
            if (currentUtteranceId == utteranceId && speaking) {
                DiagLog.log(ctx, "focus_watchdog", mapOf("id" to utteranceId, "waitMs" to delayMs))
                currentUtteranceId = null
                speaking = false
                abandonFocus(ctx, "watchdog")
                drain(ctx)
            }
        }
        watchdogRunnable = r
        mainHandler.postDelayed(r, delayMs)
    }

    private fun cancelWatchdog() {
        watchdogRunnable?.let { mainHandler.removeCallbacks(it) }
        watchdogRunnable = null
    }
}

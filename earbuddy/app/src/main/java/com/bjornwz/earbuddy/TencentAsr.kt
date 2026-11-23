package com.bjornwz.earbuddy

import android.content.Context
import android.os.Handler
import android.os.Looper
import java.io.ByteArrayOutputStream
import java.net.InetAddress
import java.net.URL
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import javax.net.ssl.HttpsURLConnection
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import kotlin.concurrent.thread

/**
 * 腾讯云实时语音识别（16k_ja / 16k_ko / 16k_zh），裸 WebSocket 直连实现。
 *
 * 0.9.3 起弃用官方 AAR SDK（AAIClient），原因已用字节码实测定位：
 * SDK 的 startAudioRecognize 是异步的（建任务、状态置 CONNECTING 后立即返回），
 * 而旧 TencentAsr 把它当阻塞调用、返回后立即 release()，等于在握手完成前掐断会话。
 * 结果：两次真机会话都在 5 秒内走完初次＋5 次重试、0 行、没有任何回调（无 onReady、
 * 无 onStartRecord、无 onFailure），被误判为「连接中断」。同一套凭据的裸 WebSocket
 * 在云端虚拟机已实测通过（2026-11-23 09:46：握手 code 0，18.17 秒日语全对），
 * 且 SDK 连的也是同一域名 asr.cloud.tencent.com/asr/v2/，所以改走裸 WebSocket。
 *
 * 协议（与 tencent_asr_test/tencent_asr_ja.py 同一套）：
 * wss://asr.cloud.tencent.com/asr/v2/{appid}?{排序参数}&signature={HmacSHA1 签名}
 * 握手 ACK 为首条 code==0 的消息（无 result）；随后每条消息带 result，
 * slice_type 0 一段话开始 / 1 进行中 / 2 结束（定稿）；{"type":"end"} 后服务端回 final=1。
 * 音频：16k 单声道 16-bit PCM，40ms 一帧（1280 字节）二进制发送。
 *
 * 对外接口与旧版一致（start/sendPcm/finish/closeNow + 四个回调），TranslateService
 * 除构造参数外不用改。密钥只存本机偏好，签名 URL 绝不记入日志（DiagLog 只记布尔、
 * 耗时与错误码）。
 */
class TencentAsr(
    private val context: Context,
    private val appId: Int,
    private val secretId: String,
    private val secretKey: String,
    private val engineModelType: String,
    private val onPartial: (String) -> Unit,
    private val onFinal: (String) -> Unit,
    private val onError: (String) -> Unit,
    private val onReady: () -> Unit
) {
    companion object {
        private const val MAX_RECONNECT = 5
        /** 退避重连：1s、2s、4s、8s、16s（旧版 1 秒固定间隔 5 秒内连冲 6 次已废弃）。 */
        private val BACKOFF_MS = longArrayOf(1000, 2000, 4000, 8000, 16000)
        private const val FRAME_BYTES = 1280 // 40ms @16k mono 16bit
        private const val MAX_PENDING = FRAME_BYTES * 250 // 约 10 秒音频，超了丢最旧
        private const val PRECHECK_TIMEOUT_MS = 3000
        private const val SILENCE_KEEPALIVE_MS = 2000L // 超过该时长无音频则补一帧静音保活
        private const val SESSION_END_TIMEOUT_MIN = 10L // 会话结束信号的安全上限

        /** 按源语言选引擎：ja→16k_ja，ko→16k_ko，其余（zh/en/auto 回落 zh）→16k_zh。 */
        fun engineFor(lang: String): String = when (lang) {
            "ja" -> "16k_ja"
            "ko" -> "16k_ko"
            else -> "16k_zh"
        }
    }

    private val main = Handler(Looper.getMainLooper())
    @Volatile private var wantRunning = false
    @Volatile private var finished = false
    @Volatile private var ws: WebSocket? = null
    @Volatile private var ackOk = false
    @Volatile private var sessionLatch: CountDownLatch? = null
    @Volatile private var nonRetryable = false
    private var reconnectAttempts = 0
    private var lastError: String? = null
    private var lastFinalText: String? = null
    private var sessionStartedAt = 0L
    private var connectStartedAt = 0L
    private var firstResultAt = 0L
    private var lastSentAt = 0L
    private var worker: Thread? = null
    private var sender: Thread? = null
    private val pending = ByteArrayOutputStream()

    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .pingInterval(20, TimeUnit.SECONDS)
        .build()

    fun start() {
        wantRunning = true
        finished = false
        reconnectAttempts = 0
        lastError = null
        lastFinalText = null
        firstResultAt = 0L
        sessionStartedAt = System.currentTimeMillis()
        worker = thread(name = "tx-ws") { runLoop() }
        startSender()
    }

    /** 会话建立前连通性自检：只做诊断、不做门禁（自检失败仍尝试直连，直连结果为准）。 */
    private fun precheck(): Pair<Boolean, String> {
        return try {
            val t0 = System.currentTimeMillis()
            val addr = InetAddress.getByName(TxWsSign.HOST)
            val conn = URL("https://${TxWsSign.HOST}/server_time").openConnection() as HttpsURLConnection
            conn.connectTimeout = PRECHECK_TIMEOUT_MS
            conn.readTimeout = PRECHECK_TIMEOUT_MS
            conn.requestMethod = "GET"
            val code = conn.responseCode
            conn.disconnect()
            val ms = System.currentTimeMillis() - t0
            (code == 200) to "dns=${addr.hostAddress} server_time=$code ${ms}ms"
        } catch (e: Exception) {
            false to "precheck fail: ${e.javaClass.simpleName}: ${e.message}"
        }
    }

    private fun runLoop() {
        val pre = precheck()
        DiagLog.logSync(context, "tx_ws_precheck", mapOf("ok" to pre.first, "detail" to pre.second))
        while (wantRunning && !finished) {
            val attempt = reconnectAttempts
            nonRetryable = false
            ackOk = false
            connectStartedAt = System.currentTimeMillis()
            DiagLog.logSync(
                context, "tx_ws_connect",
                mapOf("attempt" to attempt, "engine" to engineModelType, "precheckOk" to pre.first)
            )
            val latch = CountDownLatch(1)
            sessionLatch = latch
            val url = TxWsSign.buildSignedUrl(
                appId.toString(), secretId, secretKey, engineModelType
            )
            val req = Request.Builder().url(url).build()
            try {
                ws = client.newWebSocket(req, wsListener(latch))
            } catch (t: Throwable) {
                lastError = "创建连接失败：${t.javaClass.simpleName}: ${t.message}"
                DiagLog.logSync(context, "tx_ws_fail", mapOf("reason" to (lastError ?: ""), "attempt" to attempt))
                latch.countDown()
            }
            // 等本轮会话结束（关闭/失败/用户停止），带安全上限防永久挂起。
            latch.await(SESSION_END_TIMEOUT_MIN, TimeUnit.MINUTES)
            sessionLatch = null
            try { ws?.cancel() } catch (e: Exception) { }
            ws = null
            if (finished || !wantRunning) break
            if (nonRetryable) break // 服务端拒绝已当场报错，不重试
            // 传输层失败：退避重连；超限则报真实原因停掉。
            reconnectAttempts++
            if (reconnectAttempts > MAX_RECONNECT) {
                val msg = lastError ?: "连接失败"
                DiagLog.logSync(
                    context, "tx_ws_giveup",
                    mapOf("attempts" to reconnectAttempts, "lastError" to msg)
                )
                main.post { onError("腾讯识别多次中断，已停止（$msg）") }
                break
            }
            val delay = BACKOFF_MS[minOf(reconnectAttempts - 1, BACKOFF_MS.size - 1)]
            DiagLog.logSync(
                context, "tx_ws_reconnect",
                mapOf("attempt" to reconnectAttempts, "delayMs" to delay, "lastError" to (lastError ?: ""))
            )
            main.post { onReady() } // 借 onReady 刷新状态为“正在听”，不弹错误
            try { Thread.sleep(delay) } catch (e: Exception) { }
            // 新会话丢弃旧音频（新 voice_id 下旧音频无意义），避免把过期语音当新会话开头。
            synchronized(pending) { pending.reset() }
        }
    }

    private fun wsListener(latch: CountDownLatch) = object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            DiagLog.logSync(
                context, "tx_ws_open",
                mapOf("latencyMs" to (System.currentTimeMillis() - connectStartedAt))
            )
            // 音频要等握手 ACK（code==0）后再发；此前音频缓存在 pending。
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            handleMessage(text)
        }

        override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
            DiagLog.log(context, "tx_ws_binary_msg", mapOf("len" to bytes.size))
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            webSocket.close(1000, null)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            DiagLog.logSync(context, "tx_ws_close", mapOf("code" to code, "reason" to reason.take(200)))
            if (!finished && wantRunning && !ackOk) {
                lastError = "连接关闭（code=$code${if (reason.isNotEmpty()) "：$reason" else ""}），未收到握手确认"
            }
            latch.countDown()
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            val httpCode = response?.code
            val msg = t.message ?: t.javaClass.simpleName
            lastError = buildString {
                append("连接失败")
                if (httpCode != null) append("（HTTP $httpCode）")
                append("：$msg")
            }
            DiagLog.logSync(
                context, "tx_ws_fail",
                mapOf(
                    "reason" to (lastError ?: ""),
                    "errorClass" to t.javaClass.simpleName,
                    "httpCode" to (httpCode ?: -1),
                    "ackOk" to ackOk
                )
            )
            latch.countDown()
        }
    }

    private fun handleMessage(text: String) {
        try {
            val obj = JSONObject(text)
            val code = obj.optInt("code", -1)
            // 任何一条 code==0 的消息都算握手通过（ACK 可能与首条 result 同序到达）。
            if (code == 0 && !ackOk) {
                ackOk = true
                DiagLog.logSync(
                    context, "tx_ws_ack",
                    mapOf("code" to 0, "latencyMs" to (System.currentTimeMillis() - connectStartedAt))
                )
                main.post { onReady() }
            }
            if (obj.optInt("final", 0) == 1) {
                DiagLog.log(context, "tx_ws_final", mapOf("code" to code))
                return
            }
            if (code != 0) {
                val serverMsg = obj.optString("message", "")
                lastError = "服务端错误 $code${if (serverMsg.isNotEmpty()) "：$serverMsg" else ""}" +
                    if (code == 6001) "（跨境服务未开通或判定异常）" else ""
                DiagLog.logSync(
                    context, "tx_ws_server_error",
                    mapOf("code" to code, "message" to serverMsg.take(200))
                )
                // 服务端拒绝（鉴权/参数/跨境）重试也没用：当场报错停掉，不空转。
                nonRetryable = true
                main.post { onError("腾讯识别失败：$lastError") }
                endSessionLatch()
                return
            }
            val result = obj.optJSONObject("result") ?: return // 无 result 的 code==0 消息即握手 ACK
            val sliceType = result.optInt("slice_type", 1)
            val sentence = result.optString("voice_text_str", "").trim()
            if (sentence.isEmpty()) return
            if (firstResultAt == 0L) {
                firstResultAt = System.currentTimeMillis()
                DiagLog.logSync(
                    context, "tx_ws_first_result",
                    mapOf("latencyMs" to (firstResultAt - sessionStartedAt), "sliceType" to sliceType)
                )
            }
            if (sliceType == 2) deliverFinal(sentence) else main.post { onPartial(sentence) }
        } catch (e: Exception) {
            DiagLog.log(context, "tx_ws_bad_msg", mapOf("error" to e.toString(), "len" to text.length))
        }
    }

    /** 喂入 16k/16bit/单声道 PCM（只入缓存，实际发包由发送线程按 40ms 节奏执行）。 */
    fun sendPcm(pcm: ByteArray) {
        if (!wantRunning || finished) return
        synchronized(pending) {
            if (pending.size() > MAX_PENDING) pending.reset() // 重连过久时丢旧音频，保实时性
            pending.write(pcm)
        }
    }

    /** 发送线程：40ms 一拍；够一帧就发，不足时超过 2 秒无音频则补一帧静音保活。 */
    private fun startSender() {
        if (sender?.isAlive == true) return
        sender = thread(name = "tx-ws-send") {
            while (wantRunning && !finished) {
                val t0 = System.currentTimeMillis()
                val w = ws
                if (w != null && ackOk) {
                    val frame: ByteArray? = synchronized(pending) {
                        val all = pending.toByteArray()
                        when {
                            all.size >= FRAME_BYTES -> {
                                val piece = all.copyOfRange(0, FRAME_BYTES)
                                pending.reset()
                                if (all.size > FRAME_BYTES) pending.write(all, FRAME_BYTES, all.size - FRAME_BYTES)
                                piece
                            }
                            all.isEmpty() && System.currentTimeMillis() - lastSentAt > SILENCE_KEEPALIVE_MS ->
                                ByteArray(FRAME_BYTES) // 静音保活
                            else -> null
                        }
                    }
                    if (frame != null) {
                        try {
                            if (w.send(frame.toByteString())) lastSentAt = System.currentTimeMillis()
                        } catch (e: Exception) {
                            DiagLog.log(context, "tx_ws_send_fail", mapOf("error" to e.toString()))
                        }
                    }
                }
                val elapsed = System.currentTimeMillis() - t0
                val wait = 40 - elapsed
                if (wait > 0) try { Thread.sleep(wait) } catch (e: Exception) { }
            }
        }
    }

    /** 正常结束：发完剩余音频，再发 {"type":"end"}，等服务端回 final 后关闭。 */
    fun finish() {
        if (finished) return
        finished = true
        wantRunning = false
        thread {
            try {
                val w = ws
                if (w != null && ackOk) {
                    val rest: ByteArray = synchronized(pending) {
                        val r = pending.toByteArray(); pending.reset(); r
                    }
                    var off = 0
                    while (off < rest.size) {
                        val n = minOf(FRAME_BYTES, rest.size - off)
                        val piece = if (n < FRAME_BYTES) rest.copyOfRange(off, rest.size) + ByteArray(FRAME_BYTES - n)
                        else rest.copyOfRange(off, off + n)
                        try { w.send(piece.toByteString()) } catch (e: Exception) { }
                        off += n
                    }
                    try { w.send("{\"type\": \"end\"}") } catch (e: Exception) { }
                    Thread.sleep(3000) // 等 final 回包
                }
            } catch (e: Exception) { }
            try { ws?.close(1000, "bye") } catch (e: Exception) { }
            endSessionLatch()
        }
    }

    fun closeNow() {
        finished = true
        wantRunning = false
        try { ws?.cancel() } catch (e: Exception) { }
        ws = null
        endSessionLatch()
        try { worker?.join(1500) } catch (e: Exception) { }
        try { sender?.join(500) } catch (e: Exception) { }
        worker = null
        sender = null
    }

    private fun endSessionLatch() {
        try { sessionLatch?.countDown() } catch (e: Exception) { }
    }

    /** 定稿去重：同一句经 onSliceSuccess(sliceType=2) 与 onSegmentSuccess 各到一次时只送一次。 */
    private fun deliverFinal(text: String) {
        synchronized(this) {
            if (text == lastFinalText) return
            lastFinalText = text
        }
        main.post { onFinal(text) }
    }
}

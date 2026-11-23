package com.bjornwz.earbuddy

import android.content.Context
import android.os.Handler
import android.os.Looper
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.util.Collections
import java.util.UUID
import java.util.concurrent.TimeUnit

/**
 * 火山「一句话识别」bigmodel_nostream：整段音频一次性识别，支持 25 个语种
 * （audio.language 指定，或 enable_auto_lang 自动检测）。
 * 实时流式接口 bigmodel_async 经官方文档与本机实测确认只支持中文+英语：
 * 2026-11-20 实测，流式传 language=ja-JP 不报错但只返回罗马音（且不准），
 * 官方文档亦注明 language 参数仅一句话识别支持——日语/韩语只能走这里。
 * 协议与 VolcAsr 同族（二进制帧+gzip），已用日语/中文样本在本机实测通过。
 * Key 与流式共用（TranslateStore "volc"）。
 * 连接说明：一句话识别按协议就是一请求一连接（服务端回完结果即关闭），
 * 无法像流式那样复用同一条 WebSocket；本类复用同一个 OkHttpClient，
 * 另加失败重试 1 次（瞬时抖动不丢整句）与 12 秒单次看门狗。
 *
 * 失败原因（v0.9.2）：回调改为 (text, errorReason)——text 为空时 errorReason
 * 一定给出人类可读的原因（连接失败/服务端拒绝码/12 秒无响应/空结果），
 * 调用方据此记诊断日志并在界面上提示，不再静默出 0 行。
 * 2026-11-23 实测教训：当天的失败全是看门狗超时（12 秒无结果帧、且无服务端
 * 错误帧），旧代码把原因丢了，日志里只剩秃秃的 asr_error，无法定位是网络、
 * 鉴权还是服务端问题——本版把 onFailure 的异常/HTTP 码、错误帧的 code/message、
 * 看门狗触发时的连接/收帧状态全部记下来。
 */
object VolcSegmentAsr {

    private const val URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream"
    private const val RESOURCE_ID = "volc.seedasr.sauc.duration"
    private const val PACKET_BYTES = 6400
    private const val MAX_ATTEMPTS = 2 // 首次 + 1 次重试
    private const val WATCHDOG_MS = 12_000L

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .pingInterval(20, TimeUnit.SECONDS)
        .build()
    private val main = Handler(Looper.getMainLooper())

    /** 进行中的连接：会话停止时 cancelAll() 立即掐断，避免停止后重试/报错还继续写日志。 */
    private val activeSockets =
        Collections.synchronizedSet(mutableSetOf<WebSocket>())

    /** 掐断全部进行中的一句话识别请求（会话停止/销毁时调）。已在跑的回调会被丢弃，不再回写。 */
    fun cancelAll() {
        val copy = synchronized(activeSockets) { activeSockets.toList() }
        activeSockets.clear()
        copy.forEach { try { it.cancel() } catch (e: Exception) { } }
    }

    /**
     * lang: "ja"/"ko"/"zh"/"en"（短码，内部转地区码）。
     * cb 在主线程回调：成功时 (text, null)；失败时 (null, errorReason)，errorReason
     * 为人类可读的原因（不含任何密钥），调用方应记日志并提示用户。
     */
    fun transcribe(ctx: Context, apiKey: String, pcm: ByteArray, lang: String, cb: (String?, String?) -> Unit) {
        transcribeAttempt(ctx, apiKey, pcm, lang, cb, attempt = 0)
    }

    private fun transcribeAttempt(ctx: Context, apiKey: String, pcm: ByteArray, lang: String, cb: (String?, String?) -> Unit, attempt: Int) {
        if (pcm.isEmpty() || apiKey.isEmpty()) { cb(null, "音频为空或火山 Key 未填"); return }
        val langFull = when (lang) {
            "ja" -> "ja-JP"
            "ko" -> "ko-KR"
            "en" -> "en-US"
            "zh" -> "zh-CN"
            else -> null // auto 等：不指定，配合 enable_auto_lang
        }
        val resultText = StringBuilder()
        var done = false
        var sawErrorFrame = false
        var errorCode = 0
        var errorMsg = ""
        var opened = false
        var frames = 0
        var socket: WebSocket? = null
        lateinit var watchdog: Runnable
        fun finish(text: String?, reason: String?) {
            if (done) return
            done = true
            main.removeCallbacks(watchdog)
            socket?.let { activeSockets.remove(it) }
            try { socket?.cancel() } catch (e: Exception) { }
            val finalText = text?.ifBlank { null }
            if (finalText == null && attempt + 1 < MAX_ATTEMPTS && !sawErrorFrame) {
                DiagLog.log(ctx, "asr_retry", mapOf(
                    "engine" to "volc_nostream", "lang" to lang,
                    "attempt" to (attempt + 1),
                    "reason" to (reason ?: "无结果").take(160)
                ))
                transcribeAttempt(ctx, apiKey, pcm, lang, cb, attempt + 1)
                return
            }
            main.post { cb(finalText, if (finalText == null) reason else null) }
        }
        val req = Request.Builder()
            .url(URL)
            .header("X-Api-Key", apiKey)
            .header("X-Api-Resource-Id", RESOURCE_ID)
            .header("X-Api-Request-Id", UUID.randomUUID().toString())
            .build()
        watchdog = Runnable {
            finish(
                resultText.toString(),
                "12 秒未收到识别结果（${if (opened) "已连接" else "未连上服务器"}，收到 $frames 帧）"
            )
        }
        main.postDelayed(watchdog, WATCHDOG_MS)
        socket = client.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                opened = true
                val payload = JSONObject().apply {
                    put("user", JSONObject().put("uid", "earbuddy"))
                    put("audio", JSONObject().apply {
                        put("format", "pcm"); put("rate", 16000); put("bits", 16)
                        put("channel", 1); put("codec", "raw")
                        if (langFull != null) put("language", langFull)
                    })
                    put("request", JSONObject().apply {
                        put("model_name", "bigmodel")
                        put("enable_itn", true)
                        put("enable_punc", true)
                        put("show_utterances", true)
                        put("result_type", "single")
                        if (langFull == null) put("enable_auto_lang", true)
                    })
                }.toString()
                val gz = VolcProto.gzip(payload.toByteArray(Charsets.UTF_8))
                webSocket.send((VolcProto.frameHeader(0x1, 0, 1, 1) + VolcProto.be32(gz.size) + gz).toByteString())
                var seq = 1
                var off = 0
                while (off < pcm.size) {
                    val end = minOf(off + PACKET_BYTES, pcm.size)
                    seq += 1
                    val g = VolcProto.gzip(pcm.copyOfRange(off, end))
                    webSocket.send(
                        (VolcProto.frameHeader(0x2, 0x1, 0, 1) + VolcProto.be32(seq) + VolcProto.be32(g.size) + g).toByteString()
                    )
                    off = end
                }
                seq += 1
                val g0 = VolcProto.gzip(ByteArray(0))
                webSocket.send(
                    (VolcProto.frameHeader(0x2, 0x3, 0, 1) + VolcProto.be32(-seq) + VolcProto.be32(g0.size) + g0).toByteString()
                )
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                val raw = bytes.toByteArray()
                frames++
                if (isErrorFrame(raw)) {
                    sawErrorFrame = true
                    val (code, msg) = parseErrorFrame(raw)
                    errorCode = code
                    errorMsg = msg
                }
                val text = parseFrame(raw) ?: return
                if (text.isNotEmpty()) {
                    synchronized(resultText) { resultText.setLength(0); resultText.append(text) }
                }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                val r = when {
                    resultText.isNotBlank() -> null
                    sawErrorFrame -> "服务端拒绝 $errorCode${if (errorMsg.isNotEmpty()) "：$errorMsg" else ""}"
                    else -> "服务端返回空结果（close $code${if (reason.isNotEmpty()) " $reason" else ""}）"
                }
                finish(resultText.toString(), r)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                val httpCode = response?.code
                val detail = "${t.javaClass.simpleName}${t.message?.let { ": $it" } ?: ""}" +
                    (if (httpCode != null) "（HTTP $httpCode）" else "")
                val r = if (sawErrorFrame) {
                    "服务端拒绝 $errorCode${if (errorMsg.isNotEmpty()) "：$errorMsg" else ""}"
                } else {
                    "连接失败：$detail"
                }
                finish(resultText.toString(), r)
            }
        })
        socket?.let { activeSockets.add(it) }
    }

    /** 错误帧（type 0xF）：服务端明确拒绝（Key/参数问题），重试无意义。 */
    private fun isErrorFrame(data: ByteArray): Boolean {
        if (data.size < 4) return false
        return (data[1].toInt() shr 4 and 0x0F) == 0xF
    }

    /** 解析错误帧：返回 (code, message)，与 VolcAsr 流式同一格式（code + JSON error 字段）。 */
    private fun parseErrorFrame(data: ByteArray): Pair<Int, String> {
        try {
            if (data.size < 4) return 0 to ""
            val headerSize = (data[0].toInt() and 0x0F) * 4
            val comp = data[2].toInt() and 0x0F
            var off = headerSize
            fun read32(): Int {
                val v = ((data[off].toInt() and 0xFF) shl 24) or ((data[off + 1].toInt() and 0xFF) shl 16) or
                    ((data[off + 2].toInt() and 0xFF) shl 8) or (data[off + 3].toInt() and 0xFF)
                off += 4
                return v
            }
            val code = read32()
            val size = read32()
            if (size <= 0 || off + size > data.size) return code to ""
            var payload = data.copyOfRange(off, off + size)
            if (comp == 1) payload = VolcProto.gunzip(payload)
            val msg = try { JSONObject(String(payload, Charsets.UTF_8)).optString("error") } catch (e: Exception) { "" }
            return code to msg
        } catch (e: Exception) {
            return 0 to ""
        }
    }

    /** 解析一帧：返回 result.text（定稿全文）；错误帧/无文本返回 null。 */
    private fun parseFrame(data: ByteArray): String? {
        try {
            if (data.size < 4) return null
            val headerSize = (data[0].toInt() and 0x0F) * 4
            val type = data[1].toInt() shr 4 and 0x0F
            val flags = data[1].toInt() and 0x0F
            val comp = data[2].toInt() and 0x0F
            var off = headerSize
            fun read32(): Int {
                val v = ((data[off].toInt() and 0xFF) shl 24) or ((data[off + 1].toInt() and 0xFF) shl 16) or
                    ((data[off + 2].toInt() and 0xFF) shl 8) or (data[off + 3].toInt() and 0xFF)
                off += 4
                return v
            }
            if (type == 0xF) return null
            if (type != 0x9) return null
            if (flags and 0x1 != 0) read32()
            val size = read32()
            if (size <= 0 || off + size > data.size) return null
            var payload = data.copyOfRange(off, off + size)
            if (comp == 1) payload = VolcProto.gunzip(payload)
            val obj = JSONObject(String(payload, Charsets.UTF_8))
            val result = obj.optJSONObject("result") ?: return null
            val sb = StringBuilder()
            val utts = result.optJSONArray("utterances")
            if (utts != null) {
                for (i in 0 until utts.length()) {
                    val u = utts.optJSONObject(i) ?: continue
                    if (u.optBoolean("definite")) sb.append(u.optString("text"))
                }
            }
            if (sb.isEmpty()) {
                val t = result.optString("text")
                if (t.isNotEmpty()) return t
                return null
            }
            return sb.toString()
        } catch (e: Exception) {
            return null
        }
    }
}

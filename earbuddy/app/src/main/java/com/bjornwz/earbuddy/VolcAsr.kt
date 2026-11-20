package com.bjornwz.earbuddy

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
import java.io.ByteArrayOutputStream
import java.util.UUID
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/**
 * 火山引擎豆包·双向流式语音识别 2.0（Seed-ASR）客户端。
 * 协议已用官方文档 + 本机实测验证：wss /api/v3/sauc/bigmodel_async，
 * 鉴权 X-Api-Key + X-Api-Resource-Id；二进制帧（4 字节头 + 可选 sequence + 载荷长度 + gzip 载荷）。
 * full request 占 sequence 1，音频帧自 2 递增，末帧负 sequence；服务端发完尾包后以 1000 关闭连接。
 *
 * 保活：服务端 8 秒收不到音频包就断开（错误 45000081）。内部音频在视频暂停/无声段
 * 没有数据产出，因此发送与采集解耦——专用发送线程按 200ms 节奏发包，不足时以静音补齐；
 * 万一仍被断开（网络抖动/服务端瞬时错误），在用户无感下自动重连。
 * Key 只存本机（TranslateStore.asrKey "volc"），只发往火山官方域名。
 */
class VolcAsr(
    private val apiKey: String,
    private val onPartial: (String) -> Unit,
    private val onFinal: (String) -> Unit,
    private val onError: (String) -> Unit,
    private val onReady: () -> Unit
) {
    companion object {
        const val RESOURCE_ID = "volc.seedasr.sauc.duration"
        private const val URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
        private const val PACKET_BYTES = 6400 // 200ms @16k mono 16bit
        private const val MAX_PENDING = PACKET_BYTES * 50 // 重连期间最多缓存约 10 秒音频
        private const val MAX_RECONNECT = 5
    }

    private val main = Handler(Looper.getMainLooper())
    @Volatile private var ws: WebSocket? = null
    @Volatile private var ready = false
    @Volatile private var wantRunning = false
    @Volatile private var finished = false
    private var seq = 1 // full request 占 1，音频从 2 起
    private val pending = ByteArrayOutputStream()
    private var sender: Thread? = null
    private var reconnectAttempts = 0

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .pingInterval(20, TimeUnit.SECONDS)
        .build()

    fun start() {
        wantRunning = true
        finished = false
        connect()
        startSender()
    }

    private fun connect() {
        val req = Request.Builder()
            .url(URL)
            .header("X-Api-Key", apiKey)
            .header("X-Api-Resource-Id", RESOURCE_ID)
            .header("X-Api-Request-Id", UUID.randomUUID().toString())
            .build()
        ws = client.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                synchronized(pending) { pending.reset(); seq = 1 }
                sendFullRequest(webSocket)
            }
            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                handleFrame(bytes.toByteArray())
            }
            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(1000, null)
            }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                ready = false
                if (wantRunning && !finished) scheduleReconnect()
            }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                ready = false
                if (wantRunning && !finished) scheduleReconnect()
                else if (!finished) main.post { onError("火山连接失败：${t.message ?: "网络异常"}") }
            }
        })
    }

    @Synchronized private fun scheduleReconnect() {
        if (!wantRunning || finished) return
        reconnectAttempts++
        if (reconnectAttempts > MAX_RECONNECT) {
            wantRunning = false
            main.post { onError("火山连接多次中断，已停止（检查网络后重试）") }
            return
        }
        main.post { onReady() } // 借 onReady 刷新状态为“正在听”，不弹错误
        thread {
            try { Thread.sleep(1000) } catch (e: Exception) { }
            if (wantRunning && !finished) {
                try { ws?.cancel() } catch (e: Exception) { }
                connect()
            }
        }
    }

    private fun sendFullRequest(webSocket: WebSocket) {
        val payload = JSONObject().apply {
            put("user", JSONObject().put("uid", "earbuddy"))
            put("audio", JSONObject().apply {
                put("format", "pcm"); put("rate", 16000); put("bits", 16)
                put("channel", 1); put("codec", "raw")
            })
            put("request", JSONObject().apply {
                put("model_name", "bigmodel")
                put("enable_itn", true)
                put("enable_punc", true)
                put("show_utterances", true)
                put("result_type", "single")
                put("enable_nonstream", true) // 二遍识别：定稿更准
                put("end_window_size", 800)
            })
        }.toString()
        val gz = VolcProto.gzip(payload.toByteArray(Charsets.UTF_8))
        val frame = VolcProto.frameHeader(0x1, 0, 1, 1) + VolcProto.be32(gz.size) + gz
        webSocket.send(frame.toByteString())
        ready = true
        reconnectAttempts = 0
        main.post { onReady() }
    }

    /** 喂入 16k/16bit/单声道 PCM（只入缓存，实际发包由发送线程按节奏执行）。 */
    fun sendPcm(pcm: ByteArray) {
        if (!wantRunning || finished) return
        synchronized(pending) {
            if (pending.size() > MAX_PENDING) pending.reset() // 重连过久时丢旧音频，保实时性
            pending.write(pcm)
        }
    }

    /**
     * 发送线程：固定 200ms 节奏。每拍取至多 200ms 音频，不足补静音——
     * 保证服务端永远按时收到下一包（防 45000081 等包超时），也对齐实时语速。
     */
    private fun startSender() {
        if (sender?.isAlive == true) return
        sender = thread {
            while (wantRunning && !finished) {
                val t0 = System.currentTimeMillis()
                val w = ws
                if (w != null && ready) {
                    val chunk: ByteArray = synchronized(pending) {
                        val all = pending.toByteArray()
                        val take = minOf(all.size, PACKET_BYTES)
                        val piece = all.copyOfRange(0, take)
                        pending.reset()
                        if (all.size > take) pending.write(all, take, all.size - take)
                        if (take < PACKET_BYTES) piece + ByteArray(PACKET_BYTES - take) else piece
                    }
                    seq += 1
                    val gz = VolcProto.gzip(chunk)
                    val frame = VolcProto.frameHeader(0x2, 0x1, 0, 1) + VolcProto.be32(seq) + VolcProto.be32(gz.size) + gz
                    try { w.send(frame.toByteString()) } catch (e: Exception) { }
                }
                val elapsed = System.currentTimeMillis() - t0
                val wait = 200 - elapsed
                if (wait > 0) try { Thread.sleep(wait) } catch (e: Exception) { }
            }
        }
    }

    /** 结束：停发送线程，剩余音频作普通包发出，再发负 sequence 末帧；服务端回完尾包后主动关闭。 */
    fun finish() {
        if (finished) return
        finished = true
        wantRunning = false
        val w = ws ?: return
        val rest: ByteArray = synchronized(pending) {
            val r = pending.toByteArray(); pending.reset(); r
        }
        if (rest.isNotEmpty() && ready) {
            seq += 1
            val gz = VolcProto.gzip(rest)
            try { w.send((VolcProto.frameHeader(0x2, 0x1, 0, 1) + VolcProto.be32(seq) + VolcProto.be32(gz.size) + gz).toByteString()) } catch (e: Exception) { }
        }
        seq += 1
        val gz = VolcProto.gzip(ByteArray(0))
        try { w.send((VolcProto.frameHeader(0x2, 0x3, 0, 1) + VolcProto.be32(-seq) + VolcProto.be32(gz.size) + gz).toByteString()) } catch (e: Exception) { }
    }

    fun closeNow() {
        finished = true
        wantRunning = false
        ready = false
        try { ws?.close(1000, "bye") } catch (e: Exception) { }
        ws = null
    }

    private fun handleFrame(data: ByteArray) {
        try {
            if (data.size < 4) return
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
            if (type == 0xF) {
                val code = read32()
                val size = read32()
                var payload = data.copyOfRange(off, off + size)
                if (comp == 1) payload = VolcProto.gunzip(payload)
                val msg = try { JSONObject(String(payload, Charsets.UTF_8)).optString("error") } catch (e: Exception) { "" }
                // 等包超时 / 服务端瞬时错误：无感重连，不打扰用户
                if (code == 45000081 || code in 55000000..55099999) {
                    ready = false
                    try { ws?.cancel() } catch (e: Exception) { }
                    scheduleReconnect()
                } else {
                    main.post { onError("火山识别错误 $code${if (msg.isNotEmpty()) "：$msg" else ""}") }
                }
                return
            }
            if (type != 0x9) return
            if (flags and 0x1 != 0) read32() // sequence
            val size = read32()
            if (size <= 0 || off + size > data.size) return
            var payload = data.copyOfRange(off, off + size)
            if (comp == 1) payload = VolcProto.gunzip(payload)
            val obj = JSONObject(String(payload, Charsets.UTF_8))
            val result = obj.optJSONObject("result") ?: return
            val utts = result.optJSONArray("utterances")
            if (utts != null && utts.length() > 0) {
                for (i in 0 until utts.length()) {
                    val u = utts.optJSONObject(i) ?: continue
                    val text = u.optString("text").trim()
                    if (text.isEmpty()) continue
                    if (u.optBoolean("definite")) main.post { onFinal(text) }
                    else main.post { onPartial(text) }
                }
            } else {
                val text = result.optString("text").trim()
                if (text.isNotEmpty()) main.post { onPartial(text) }
            }
        } catch (e: Exception) {
            // 单帧解析失败不中断会话
        }
    }
}

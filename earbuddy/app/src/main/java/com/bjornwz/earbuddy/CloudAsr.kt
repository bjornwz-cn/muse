package com.bjornwz.earbuddy

import android.content.Context
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.net.HttpURLConnection
import java.net.URL

/**
 * 云端识别渠道（OpenAI 兼容 /audio/transcriptions 协议）：
 * 本地 Vosk 负责切分与兜底，切出的音频段送云端 Whisper 类模型重识别，准确率质变。
 * Key 只存本机（TranslateStore），只发往用户所选渠道。
 */
object CloudAsr {

    data class Channel(
        val id: String, val name: String, val base: String, val model: String,
        val keyUrl: String, val note: String
    )

    val GROQ = Channel(
        "groq", "Groq Whisper", "https://api.groq.com/openai/v1", "whisper-large-v3-turbo",
        "https://console.groq.com/keys", "免费额度（需外网注册）；音乐/噪声耐受强，速度快"
    )
    val SILICONFLOW = Channel(
        "siliconflow", "硅基流动 SenseVoice", "https://api.siliconflow.cn/v1",
        "FunAudioLLM/SenseVoiceSmall",
        "https://cloud.siliconflow.cn/account/ak", "国内直连；新用户有免费额度，中文识别强"
    )
    val OPENAI = Channel(
        "openai", "OpenAI Whisper", "https://api.openai.com/v1", "whisper-1",
        "https://platform.openai.com/api-keys", "按量付费约 $0.006/分钟；需外网"
    )
    val PRESETS = listOf(GROQ, SILICONFLOW, OPENAI)

    fun channelOf(ctx: Context, id: String): Channel? = when (id) {
        "groq" -> GROQ
        "siliconflow" -> SILICONFLOW
        "openai" -> OPENAI
        "custom" -> {
            val base = TranslateStore.customAsrBase(ctx).trimEnd('/')
            val model = TranslateStore.customAsrModel(ctx)
            if (base.isEmpty() || model.isEmpty()) null
            else Channel("custom", "自定义渠道", base, model, "", "OpenAI 兼容接口")
        }
        else -> null
    }

    /** 当前是否启用云端识别（引擎为渠道且 Key 已填）。 */
    fun activeChannel(ctx: Context): Channel? {
        val engine = TranslateStore.asrEngine(ctx)
        if (engine == "vosk") return null
        val ch = channelOf(ctx, engine) ?: return null
        if (TranslateStore.asrKey(ctx, ch.id).isEmpty()) return null
        return ch
    }

    /** 上传 wav 段识别。cb 在子线程回调：成功返回文本，失败返回 null（调用方回退离线文本）。 */
    fun transcribe(ctx: Context, ch: Channel, wav: ByteArray, lang: String, cb: (String?) -> Unit) {
        Thread {
            try {
                val key = TranslateStore.asrKey(ctx.applicationContext, ch.id)
                val conn = URL(ch.base + "/audio/transcriptions").openConnection() as HttpURLConnection
                conn.requestMethod = "POST"
                conn.connectTimeout = 15000
                conn.readTimeout = 30000
                conn.doOutput = true
                val boundary = "EarBuddy" + System.currentTimeMillis()
                conn.setRequestProperty("Authorization", "Bearer $key")
                conn.setRequestProperty("Content-Type", "multipart/form-data; boundary=$boundary")
                val out = ByteArrayOutputStream()
                fun field(name: String, value: String) {
                    out.write("--$boundary\r\nContent-Disposition: form-data; name=\"$name\"\r\n\r\n$value\r\n".toByteArray())
                }
                out.write("--$boundary\r\nContent-Disposition: form-data; name=\"file\"; filename=\"seg.wav\"\r\nContent-Type: audio/wav\r\n\r\n".toByteArray())
                out.write(wav)
                out.write("\r\n".toByteArray())
                field("model", ch.model)
                if (lang != "auto") field("language", lang)
                field("response_format", "json")
                out.write("--$boundary--\r\n".toByteArray())
                conn.outputStream.use { it.write(out.toByteArray()) }
                val code = conn.responseCode
                val body = (if (code in 200..299) conn.inputStream else conn.errorStream)
                    ?.readBytes()?.toString(Charsets.UTF_8) ?: ""
                if (code !in 200..299) { cb(null); return@Thread }
                val text = JSONObject(body).optString("text").trim()
                cb(if (text.isEmpty()) null else text)
            } catch (e: Exception) {
                cb(null)
            }
        }.start()
    }
}

package com.bjornwz.earbuddy

import android.content.Context
import android.os.Handler
import android.os.Looper
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.util.concurrent.Executors

/**
 * 可插拔翻译引擎：默认微软翻译（Edge 通道，免 Key、国内可直连）；
 * 谷歌（国内需代理）与 DeepL（需 Key）可在翻译页「翻译服务」切换。失败回调 null。
 */
object Translator {

    private val io = Executors.newCachedThreadPool()
    private val main = Handler(Looper.getMainLooper())

    private inline fun safe(f: () -> String?): String? =
        try { f() } catch (e: Exception) { null }

    fun translate(ctx: Context, text: String, from: String, to: String, cb: (String?) -> Unit) {
        val provider = TranslateStore.provider(ctx)
        val key = TranslateStore.deeplKey(ctx)
        io.execute {
            // 免 Key 通道互为兜底：首选失败自动换另一个，不让整段丢译文
            val mtKey = TranslateStore.volcMtAk(ctx)
            val out = when {
                provider == "deepl" && key.isNotBlank() -> safe { deepl(text, from, to, key) }
                provider == "llm" ->
                    safe { LlmTranslate.translateSync(ctx, text, from, to) }
                        ?: (if (mtKey.isNotBlank()) safe { volcMt(text, from, to, mtKey) } else null)
                        ?: safe { microsoft(text, from, to) } ?: safe { google(text, from, to) }
                provider == "volc" && mtKey.isNotBlank() ->
                    safe { volcMt(text, from, to, mtKey) }
                        ?: safe { microsoft(text, from, to) } ?: safe { google(text, from, to) }
                provider == "google" -> safe { google(text, from, to) } ?: safe { microsoft(text, from, to) }
                else -> safe { microsoft(text, from, to) } ?: safe { google(text, from, to) }
            }
            main.post { cb(out) }
        }
    }

    // ——— 微软翻译（Edge 免费通道）：先取短期令牌（约 10 分钟），再调 Translator API ———
    @Volatile private var msToken: String? = null
    @Volatile private var msTokenExp: Long = 0

    private fun msLang(c: String): String? = when (c) {
        "auto" -> null
        "zh" -> "zh-Hans"
        else -> c // en/ja/ko 相同
    }

    private fun fetchMsToken(): String? {
        val conn = URL("https://edge.microsoft.com/translate/auth").openConnection() as HttpURLConnection
        conn.connectTimeout = 5000
        conn.readTimeout = 5000
        conn.setRequestProperty(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36 Edg/120.0"
        )
        if (conn.responseCode != 200) return null
        val t = conn.inputStream.bufferedReader().readText().trim()
        if (t.length < 20) return null
        msToken = t
        msTokenExp = System.currentTimeMillis() + 8 * 60_000
        return t
    }

    private fun microsoft(text: String, from: String, to: String): String? {
        val token = (if (System.currentTimeMillis() < msTokenExp) msToken else null)
            ?: fetchMsToken() ?: return null
        val first = msCall(text, from, to, token)
        if (first != null) return first
        // 令牌可能被提前作废：强刷一次再试
        val t2 = fetchMsToken() ?: return null
        return msCall(text, from, to, t2)
    }

    private fun msCall(text: String, from: String, to: String, token: String): String? {
        val sb = StringBuilder("https://api.cognitive.microsofttranslator.com/translate?api-version=3.0")
        msLang(from)?.let { sb.append("&from=").append(it) }
        sb.append("&to=").append(msLang(to) ?: "en")
        val conn = URL(sb.toString()).openConnection() as HttpURLConnection
        conn.requestMethod = "POST"
        conn.doOutput = true
        conn.connectTimeout = 5000
        conn.readTimeout = 5000
        conn.setRequestProperty("Authorization", "Bearer $token")
        conn.setRequestProperty("Content-Type", "application/json")
        val body = JSONArray().put(JSONObject().put("Text", text)).toString()
        conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
        if (conn.responseCode != 200) return null
        val body2 = conn.inputStream.bufferedReader().readText()
        return JSONArray(body2).getJSONObject(0)
            .getJSONArray("translations").getJSONObject(0)
            .getString("text").ifBlank { null }
    }

    // ——— 火山机器翻译（语音技术控制台 MATX：API Key 鉴权，与语音识别同套 Key 格式）———
    // POST openspeech.bytedance.com/api/v3/machine_translation/matx_translate，Resource-Id: volc.speech.mt
    // 2026-11-08 muse 实测双向通过；响应 data.translation_list[0].translation，计费按 token。
    private fun volcMt(text: String, from: String, to: String, apiKey: String): String? {
        val bodyObj = JSONObject()
            .put("target_language", to)
            .put("text_list", JSONArray().put(text))
        if (from != "auto") bodyObj.put("source_language", from)
        val body = bodyObj.toString()
        val conn = URL("https://openspeech.bytedance.com/api/v3/machine_translation/matx_translate")
            .openConnection() as HttpURLConnection
        conn.requestMethod = "POST"
        conn.doOutput = true
        conn.connectTimeout = 5000
        conn.readTimeout = 8000
        conn.setRequestProperty("Content-Type", "application/json")
        conn.setRequestProperty("x-api-key", apiKey)
        conn.setRequestProperty("X-Api-Resource-Id", "volc.speech.mt")
        conn.setRequestProperty(
            "X-Api-Request-Id",
            java.util.UUID.randomUUID().toString().replace("-", "")
        )
        conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
        if (conn.responseCode != 200) return null
        val root = JSONObject(conn.inputStream.bufferedReader().readText())
        if (root.optInt("code") != 20000000) return null
        val list = root.optJSONObject("data")?.optJSONArray("translation_list") ?: return null
        if (list.length() == 0) return null
        return list.getJSONObject(0).optString("translation").ifBlank { null }
    }

    private fun google(text: String, from: String, to: String): String? {
        val u = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=$from&tl=$to&dt=t&q=" +
            URLEncoder.encode(text, "UTF-8")
        val conn = URL(u).openConnection() as HttpURLConnection
        conn.connectTimeout = 5000
        conn.readTimeout = 5000
        conn.setRequestProperty("User-Agent", "Mozilla/5.0")
        val body = conn.inputStream.bufferedReader().readText()
        val segs = JSONArray(body).getJSONArray(0)
        val sb = StringBuilder()
        for (i in 0 until segs.length()) sb.append(segs.getJSONArray(i).getString(0))
        return sb.toString().ifBlank { null }
    }

    private fun deepl(text: String, from: String, to: String, key: String): String? {
        val host = if (key.endsWith(":fx")) "https://api-free.deepl.com" else "https://api.deepl.com"
        val conn = URL("$host/v2/translate").openConnection() as HttpURLConnection
        conn.requestMethod = "POST"
        conn.doOutput = true
        conn.connectTimeout = 5000
        conn.readTimeout = 5000
        conn.setRequestProperty("Authorization", "DeepL-Auth-Key $key")
        conn.setRequestProperty("Content-Type", "application/x-www-form-urlencoded")
        val src = if (from == "zh") "ZH" else "EN"
        val tgt = if (to == "zh") "ZH-HANS" else "EN-US"
        val form = "text=" + URLEncoder.encode(text, "UTF-8") + "&source_lang=$src&target_lang=$tgt"
        conn.outputStream.use { it.write(form.toByteArray(Charsets.UTF_8)) }
        val body = conn.inputStream.bufferedReader().readText()
        return JSONObject(body).getJSONArray("translations").getJSONObject(0).getString("text")
    }
}

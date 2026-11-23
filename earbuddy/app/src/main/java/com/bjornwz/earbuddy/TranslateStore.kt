package com.bjornwz.earbuddy

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** 翻译会话状态（供冲突仲裁）、期间暂存通知、会话记录存取、翻译引擎设置。 */
object TranslateStore {

    private fun prefs(ctx: Context) =
        ctx.getSharedPreferences("translate", Context.MODE_PRIVATE)

    /** 语言代码的显示名（方向与记录标签用；与离线模型无关，模型只有中英）。 */
    fun langName(code: String): String = when (code) {
        "zh" -> "中文"
        "en" -> "English"
        "ja" -> "日本語"
        "ko" -> "한국어"
        "auto" -> "自动识别"
        else -> code
    }

    // ——— 会话状态（NotifyListener 据此决定通知只存不播）———
    fun isActive(ctx: Context) = prefs(ctx).getBoolean("active", false)
    fun setActive(ctx: Context, on: Boolean) =
        prefs(ctx).edit().putBoolean("active", on).apply()

    // ——— 翻译期间暂存的通知 ———
    fun defer(ctx: Context, line: String, important: Boolean) {
        val arr = JSONArray(prefs(ctx).getString("deferred", "[]"))
        arr.put(JSONObject().put("line", line).put("imp", important))
        prefs(ctx).edit().putString("deferred", arr.toString()).apply()
    }

    fun takeDeferred(ctx: Context): List<Pair<String, Boolean>> {
        val arr = JSONArray(prefs(ctx).getString("deferred", "[]"))
        val out = mutableListOf<Pair<String, Boolean>>()
        for (i in 0 until arr.length()) {
            val o = arr.getJSONObject(i)
            out.add(o.getString("line") to o.optBoolean("imp"))
        }
        prefs(ctx).edit().remove("deferred").apply()
        return out
    }

    // ——— 会话记录 ———
    data class Entry(val src: String, val dst: String)
    data class Record(
        val id: Long, val startedAt: Long,
        val from: String, val to: String,
        val entries: List<Entry>,
        val audioId: Long = 0 // !=0 时本场有录音存档（SessionRecorder，id 即 startedAt）
    )

    fun saveRecord(ctx: Context, rec: Record) {
        val dir = File(ctx.filesDir, "records").apply { mkdirs() }
        val o = JSONObject()
            .put("id", rec.id).put("startedAt", rec.startedAt)
            .put("from", rec.from).put("to", rec.to)
        if (rec.audioId != 0L) o.put("audio", rec.audioId)
        val a = JSONArray()
        rec.entries.forEach { a.put(JSONObject().put("s", it.src).put("d", it.dst)) }
        o.put("entries", a)
        File(dir, "${rec.id}.json").writeText(o.toString())
    }

    fun listRecords(ctx: Context): List<Record> {
        val dir = File(ctx.filesDir, "records")
        if (!dir.exists()) return emptyList()
        return dir.listFiles()?.sortedByDescending { it.name }?.mapNotNull { f ->
            try {
                val o = JSONObject(f.readText())
                val a = o.getJSONArray("entries")
                val es = mutableListOf<Entry>()
                for (i in 0 until a.length()) {
                    val e = a.getJSONObject(i)
                    es.add(Entry(e.getString("s"), e.getString("d")))
                }
                Record(o.getLong("id"), o.getLong("startedAt"), o.getString("from"), o.getString("to"), es, o.optLong("audio", 0))
            } catch (e: Exception) { null }
        } ?: emptyList()
    }

    // ——— 翻译引擎设置 ———
    fun provider(ctx: Context): String {
        val p = prefs(ctx)
        // v0.4.8 迁移：谷歌翻译国内不可直连，默认引擎一次性切到微软（曾显式选 DeepL 的不覆盖）
        if (!p.getBoolean("mt_migrated_048", false)) {
            val cur = p.getString("provider", null)
            val next = if (cur == null || cur == "google") "microsoft" else cur
            p.edit().putString("provider", next).putBoolean("mt_migrated_048", true).apply()
            return next
        }
        return p.getString("provider", "microsoft") ?: "microsoft"
    }
    fun setProvider(ctx: Context, v: String) = prefs(ctx).edit().putString("provider", v).apply()
    fun deeplKey(ctx: Context) = prefs(ctx).getString("deepl_key", "") ?: ""
    fun setDeeplKey(ctx: Context, v: String) = prefs(ctx).edit().putString("deepl_key", v).apply()
    // ——— 朗读输出：auto 自动（有耳机走耳机、没耳机走手机外放，默认）/ earbuds 仅耳机 / off 不朗读 ———
    // v0.8.2 起：旧 speak_dst 开关把「送到耳机」和「开/关」混为一谈，关掉即全程静音，用户反馈后改为三态。
    fun speakDest(ctx: Context) = prefs(ctx).getString("speak_dest", "auto") ?: "auto"
    fun setSpeakDest(ctx: Context, v: String) = prefs(ctx).edit().putString("speak_dest", v).apply()

    /** 本句译文是否朗读（朗读走 USAGE_MEDIA 系统路由：A2DP 在连时进耳机，否则手机外放）。 */
    fun shouldSpeak(ctx: Context): Boolean = when (speakDest(ctx)) {
        "off" -> false
        "earbuds" -> Speaker.btAudioConnected(ctx)
        else -> true
    }

    // ——— 收音来源：phone 手机麦克风（默认）/ bt 蓝牙耳机麦克风 ———
    fun micSource(ctx: Context) = prefs(ctx).getString("mic_source", "phone") ?: "phone"
    fun setMicSource(ctx: Context, v: String) = prefs(ctx).edit().putString("mic_source", v).apply()

    // ——— 识别精度：std 标准小模型 / high 高精度大模型 ———
    fun modelTier(ctx: Context) = prefs(ctx).getString("model_tier", "std") ?: "std"
    fun setModelTier(ctx: Context, v: String) = prefs(ctx).edit().putString("model_tier", v).apply()

    // ——— 识别引擎：vosk 离线（默认）/ 云端渠道 id（groq/siliconflow/openai/custom）———
    fun asrEngine(ctx: Context) = prefs(ctx).getString("asr_engine", "vosk") ?: "vosk"
    fun setAsrEngine(ctx: Context, v: String) = prefs(ctx).edit().putString("asr_engine", v).apply()
    fun asrKey(ctx: Context, channel: String) =
        prefs(ctx).getString("asr_key_$channel", "") ?: ""
    fun setAsrKey(ctx: Context, channel: String, v: String) =
        prefs(ctx).edit().putString("asr_key_$channel", v).apply()
    // ——— 腾讯云实时 ASR（引擎 id "tx"）：三值分开存（AppId / SecretId / SecretKey），
    // 存本机 SharedPreferences，与火山 Key 同级；源码/资源/文档中不出现真实值。———
    fun txAppId(ctx: Context) = prefs(ctx).getString("tx_appid", "") ?: ""
    fun txSecretId(ctx: Context) = prefs(ctx).getString("tx_secret_id", "") ?: ""
    fun txSecretKey(ctx: Context) = prefs(ctx).getString("tx_secret_key", "") ?: ""
    fun setTxCreds(ctx: Context, appId: String, secretId: String, secretKey: String) =
        prefs(ctx).edit().putString("tx_appid", appId)
            .putString("tx_secret_id", secretId)
            .putString("tx_secret_key", secretKey).apply()
    fun txConfigured(ctx: Context) = txAppId(ctx).isNotEmpty() &&
        txSecretId(ctx).isNotEmpty() && txSecretKey(ctx).isNotEmpty()
    fun customAsrBase(ctx: Context) = prefs(ctx).getString("asr_custom_base", "") ?: ""
    fun customAsrModel(ctx: Context) = prefs(ctx).getString("asr_custom_model", "") ?: ""
    fun setCustomAsr(ctx: Context, base: String, model: String) =
        prefs(ctx).edit().putString("asr_custom_base", base)
            .putString("asr_custom_model", model).apply()

    /** 一条记录格式化为可分享/导出的纯文本。 */
    fun recordText(rec: Record): String {
        val fmt = SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.CHINA)
        val sb = StringBuilder()
        sb.append("【翻译记录 ").append(fmt.format(Date(rec.startedAt))).append("】")
            .append(
                when {
                    rec.from == "auto" && rec.to == "auto" -> "双向自动"
                    rec.from == "auto" -> "自动识别 → ${TranslateStore.langName(rec.to)}"
                    else -> "${TranslateStore.langName(rec.from)} → ${TranslateStore.langName(rec.to)}"
                }
            )
            .append("（").append(rec.entries.size).append(" 句）\n")
        if (rec.audioId != 0L) sb.append("（本场含录音存档）\n")
        rec.entries.forEachIndexed { i, e ->
            sb.append(i + 1).append(". ").append(e.src).append('\n')
                .append("    ").append(e.dst).append('\n')
        }
        return sb.toString()
    }

    /** 删除一条记录及其录音文件。 */
    fun deleteRecord(ctx: Context, rec: Record) {
        try { File(File(ctx.filesDir, "records"), "${rec.id}.json").delete() } catch (e: Exception) { }
        if (rec.audioId != 0L) {
            try { SessionRecorder.wavFor(ctx, rec.audioId).delete() } catch (e: Exception) { }
            try { SessionRecorder.m4aFor(ctx, rec.audioId).delete() } catch (e: Exception) { }
        }
    }

    /** 清空全部记录与全部录音文件。 */
    fun clearAllRecords(ctx: Context) {
        try { File(ctx.filesDir, "records").listFiles()?.forEach { it.delete() } } catch (e: Exception) { }
        try { SessionRecorder.dir(ctx).listFiles()?.forEach { it.delete() } } catch (e: Exception) { }
    }

    // ——— 翻译方向（后台服务也要读，须持久化）：默认 中→英 ———
    fun dirFrom(ctx: Context) = prefs(ctx).getString("dir_from", "zh") ?: "zh"
    fun dirTo(ctx: Context) = prefs(ctx).getString("dir_to", "en") ?: "en"
    fun setDir(ctx: Context, from: String, to: String) =
        prefs(ctx).edit().putString("dir_from", from).putString("dir_to", to).apply()

    // ——— 静音自动停：连续无声音/无识别结果达到该分钟数自动停（防忘关持续扣费）；0=关闭，默认 5 ———
    fun silenceStopMinutes(ctx: Context) = prefs(ctx).getInt("silence_stop_min", 5)
    fun setSilenceStopMinutes(ctx: Context, v: Int) =
        prefs(ctx).edit().putInt("silence_stop_min", v).apply()

    // ——— 会话录音存档：翻译时同步保存本场音频（默认开）———
    fun recordAudio(ctx: Context) = prefs(ctx).getBoolean("record_audio", true)
    fun setRecordAudio(ctx: Context, on: Boolean) =
        prefs(ctx).edit().putBoolean("record_audio", on).apply()

    // ——— 火山机器翻译（语音技术 MATX）API Key：存 volc_mt_ak 一个值即可 ———
    fun volcMtAk(ctx: Context) = prefs(ctx).getString("volc_mt_ak", "") ?: ""
    fun setVolcMtKey(ctx: Context, ak: String) =
        prefs(ctx).edit().putString("volc_mt_ak", ak).remove("volc_mt_sk").apply()

    // ——— 大模型上下文翻译（LlmTranslate）设置 ———
    fun llmPreset(ctx: Context) = prefs(ctx).getString("llm_preset", "siliconflow") ?: "siliconflow"
    fun llmKey(ctx: Context) = prefs(ctx).getString("llm_key", "") ?: ""
    fun llmModel(ctx: Context) = prefs(ctx).getString("llm_model", "") ?: ""
    fun setLlmConfig(ctx: Context, preset: String, key: String, model: String) =
        prefs(ctx).edit().putString("llm_preset", preset)
            .putString("llm_key", key).putString("llm_model", model).apply()
}

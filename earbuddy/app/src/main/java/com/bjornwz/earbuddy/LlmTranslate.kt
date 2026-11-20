package com.bjornwz.earbuddy

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * 大模型上下文翻译（OpenAI 兼容 /chat/completions）：
 * 与逐句机翻的区别——每次翻译都带上「领域背景 + 前 3 句的原文/译文对照」，
 * 术语前后一致、指代不丢、风格随领域（医疗/商务/财经/技术）自动适配。
 * 领域由会话开头几句自动判定（TranslateService 喂 probe 文本进来）。
 * 同步调用（在 Translator 的 io 线程里跑），失败返回 null 由调用方兜底。
 */
object LlmTranslate {

    data class Preset(val id: String, val name: String, val base: String, val model: String, val note: String)

    val SILICONFLOW = Preset(
        "siliconflow", "硅基流动", "https://api.siliconflow.cn/v1", "Qwen/Qwen2.5-7B-Instruct",
        "国内直连；有免费模型档；若已在识别渠道填过硅基流动 Key 会自动复用"
    )
    val DEEPSEEK = Preset(
        "deepseek", "DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat",
        "国内直连；按量付费，价格很低，质量好"
    )
    val ARK = Preset(
        "ark", "火山方舟·豆包", "https://ark.cn-beijing.volces.com/api/v3", "",
        "模型一栏填方舟的推理接入点 ID（ep- 开头）"
    )
    val PRESETS = listOf(SILICONFLOW, DEEPSEEK, ARK)

    fun presetOf(ctx: Context): Preset =
        PRESETS.firstOrNull { it.id == TranslateStore.llmPreset(ctx) } ?: SILICONFLOW

    fun effectiveKey(ctx: Context): String {
        val k = TranslateStore.llmKey(ctx)
        if (k.isNotBlank()) return k
        // 硅基流动与识别渠道同账号同 Key，填过识别就不用再填一遍
        if (presetOf(ctx).id == "siliconflow") return TranslateStore.asrKey(ctx, "siliconflow")
        return ""
    }

    fun effectiveModel(ctx: Context): String {
        val m = TranslateStore.llmModel(ctx)
        return if (m.isNotBlank()) m else presetOf(ctx).model
    }

    fun available(ctx: Context): Boolean =
        effectiveKey(ctx).isNotBlank() && effectiveModel(ctx).isNotBlank()

    // ——— 会话上下文（服务维护）———
    @Volatile var domain: String = "通用"
    private val history = mutableListOf<Pair<String, String>>()

    @Synchronized fun resetSession() {
        history.clear()
        domain = "通用"
    }

    @Synchronized fun notePair(src: String, dst: String) {
        if (dst.startsWith("（")) return // 翻译失败的占位不进上下文
        history.add(src to dst)
        while (history.size > 3) history.removeAt(0)
    }

    @Synchronized private fun contextPairs(): List<Pair<String, String>> = history.toList()

    /** 领域自动判定：会话开头文本命中关键词即归类，命中不了保持通用。 */
    fun detectDomain(text: String): String {
        fun hit(vararg words: String) = words.any { text.contains(it) }
        return when {
            hit("患者", "手术", "临床", "器械", "诊断", "治疗", "医院", "医生", "导管", "支架", "科室", "门诊", "病例") -> "医疗健康"
            hit("股票", "营收", "利润", "投资", "股价", "基金", "财报") -> "财经"
            hit("合同", "客户", "报价", "订单", "市场", "销售", "渠道", "业绩", "季度", "合作", "经销商") -> "商务会议"
            hit("系统", "数据", "接口", "服务器", "软件", "模型", "算法") -> "技术"
            else -> "通用"
        }
    }

    private fun langName(code: String): String = when (code) {
        "zh" -> "中文"; "en" -> "英文"; "ja" -> "日文"; "ko" -> "韩文"; else -> code
    }

    fun translateSync(ctx: Context, text: String, from: String, to: String): String? {
        val preset = presetOf(ctx)
        val key = effectiveKey(ctx)
        val model = effectiveModel(ctx)
        if (key.isBlank() || model.isBlank()) return null
        val system = "你是专业同声传译员。把用户给出的${langName(from)}文本翻译成${langName(to)}。" +
            "当前领域背景：$domain。规则：只输出译文，不要解释、不要加引号；" +
            "术语与前文保持一致；指代要明确；口语译得自然通顺，正式内容保持正式。"
        val messages = JSONArray()
        messages.put(JSONObject().put("role", "system").put("content", system))
        for ((s, d) in contextPairs()) {
            messages.put(JSONObject().put("role", "user").put("content", s))
            messages.put(JSONObject().put("role", "assistant").put("content", d))
        }
        messages.put(JSONObject().put("role", "user").put("content", text))
        val body = JSONObject()
            .put("model", model)
            .put("temperature", 0.2)
            .put("messages", messages)
            .toString()
        val conn = URL(preset.base + "/chat/completions").openConnection() as HttpURLConnection
        conn.requestMethod = "POST"
        conn.doOutput = true
        conn.connectTimeout = 6000
        conn.readTimeout = 15000
        conn.setRequestProperty("Authorization", "Bearer $key")
        conn.setRequestProperty("Content-Type", "application/json")
        conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
        if (conn.responseCode != 200) return null
        val root = JSONObject(conn.inputStream.bufferedReader().readText())
        val content = root.optJSONArray("choices")?.optJSONObject(0)
            ?.optJSONObject("message")?.optString("content")?.trim()
            ?: return null
        return content.trim('"', '「', '」').ifBlank { null }
    }
}

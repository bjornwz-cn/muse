package com.bjornwz.earbuddy

import android.content.Context
import android.content.res.ColorStateList
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.LinearLayout
import android.widget.Switch
import android.widget.TextView

/** 全局视觉规范（v0.2 起）：浅灰底 + 白卡片 + 深青主色，圆角与间距统一走这里。 */
object Ui {
    const val ACCENT = 0xFF0E7C66.toInt()
    const val ACCENT_DARK = 0xFF0A5C4B.toInt()
    const val BG = 0xFFF4F6F7.toInt()
    const val CARD = Color.WHITE
    const val TEXT = 0xFF1B1E1F.toInt()
    const val SUBTEXT = 0xFF687174.toInt()
    const val DANGER = 0xFFC0392B.toInt()
    const val OK = 0xFF1E9E6A.toInt()
    const val WARN = 0xFFD9930D.toInt()

    fun dp(ctx: Context, v: Int): Int = (v * ctx.resources.displayMetrics.density).toInt()

    fun rounded(color: Int, radiusDp: Int, ctx: Context): GradientDrawable =
        GradientDrawable().apply {
            setColor(color)
            cornerRadius = dp(ctx, radiusDp).toFloat()
        }

    fun card(ctx: Context): LinearLayout = LinearLayout(ctx).apply {
        orientation = LinearLayout.VERTICAL
        background = rounded(CARD, 16, ctx)
        setPadding(dp(ctx, 18), dp(ctx, 16), dp(ctx, 18), dp(ctx, 16))
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
        ).apply { bottomMargin = dp(ctx, 14) }
        elevation = dp(ctx, 2).toFloat()
    }

    fun title(ctx: Context, text: String, size: Float = 20f): TextView = TextView(ctx).apply {
        this.text = text
        textSize = size
        setTextColor(TEXT)
        typeface = android.graphics.Typeface.DEFAULT_BOLD
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
        ).apply { bottomMargin = dp(ctx, 8) }
    }

    fun body(ctx: Context, text: String, size: Float = 15f, color: Int = TEXT): TextView =
        TextView(ctx).apply {
            this.text = text
            textSize = size
            setTextColor(color)
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply { bottomMargin = dp(ctx, 6) }
        }

    /** 主按钮：主色实心；次按钮：浅灰。 */
    fun button(ctx: Context, text: String, primary: Boolean = false, onClick: () -> Unit): Button =
        Button(ctx).apply {
            this.text = text
            textSize = 16f
            isAllCaps = false
            if (primary) {
                setTextColor(Color.WHITE)
                background = rounded(ACCENT, 12, ctx)
            } else {
                setTextColor(TEXT)
                background = rounded(0xFFE9EDED.toInt(), 12, ctx)
            }
            setPadding(0, dp(ctx, 6), 0, dp(ctx, 6))
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply { bottomMargin = dp(ctx, 10) }
            setOnClickListener { onClick() }
        }

    fun styledSwitch(ctx: Context): Switch = Switch(ctx).apply {
        thumbTintList = ColorStateList(
            arrayOf(intArrayOf(android.R.attr.state_checked), intArrayOf()),
            intArrayOf(ACCENT, 0xFFB9C0C2.toInt())
        )
        trackTintList = ColorStateList(
            arrayOf(intArrayOf(android.R.attr.state_checked), intArrayOf()),
            intArrayOf(0xFF9FD4C6.toInt(), 0xFFD8DEDE.toInt())
        )
    }

    // ——— 设置页组件（v0.6 布局规范）：分组标题 + 左标签右值的设置行 + 开关行 ———

    class Row(val view: View, val value: TextView)
    class SwitchRow(val view: View, val switch: Switch)

    fun sectionTitle(ctx: Context, text: String): TextView = TextView(ctx).apply {
        this.text = text
        textSize = 12.5f
        setTextColor(SUBTEXT)
        typeface = android.graphics.Typeface.DEFAULT_BOLD
        letterSpacing = 0.06f
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
        ).apply { topMargin = dp(ctx, 14); bottomMargin = dp(ctx, 2) }
    }

    /** 设置行：左侧灰标签 + 右侧深色值（右对齐、超长省略）+ ›，整行可点、带按压反馈。 */
    fun settingRow(ctx: Context, label: String, value: String, onClick: () -> Unit): Row {
        val valueView = TextView(ctx).apply {
            text = value
            textSize = 15f
            setTextColor(TEXT)
            gravity = android.view.Gravity.END
            maxLines = 1
            ellipsize = android.text.TextUtils.TruncateAt.END
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        val row = LinearLayout(ctx).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = android.view.Gravity.CENTER_VERTICAL
            setPadding(dp(ctx, 2), dp(ctx, 13), dp(ctx, 2), dp(ctx, 13))
            val tv = android.util.TypedValue()
            ctx.theme.resolveAttribute(android.R.attr.selectableItemBackground, tv, true)
            setBackgroundResource(tv.resourceId)
            addView(TextView(ctx).apply {
                text = label
                textSize = 14f
                setTextColor(SUBTEXT)
                layoutParams = LinearLayout.LayoutParams(dp(ctx, 84), ViewGroup.LayoutParams.WRAP_CONTENT)
            })
            addView(valueView)
            addView(TextView(ctx).apply {
                text = "›"
                textSize = 20f
                setTextColor(0xFFB9C0C2.toInt())
                setPadding(dp(ctx, 6), 0, dp(ctx, 2), 0)
            })
            setOnClickListener { onClick() }
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
            )
        }
        return Row(row, valueView)
    }

    /** 开关行：左标签 + 右开关（先设值后挂监听，初始化不触发回调）。 */
    fun switchRow(ctx: Context, text: String, checked: Boolean, onChange: (Boolean) -> Unit): SwitchRow {
        val sw = styledSwitch(ctx)
        sw.isChecked = checked
        sw.setOnCheckedChangeListener { _, c -> onChange(c) }
        val row = LinearLayout(ctx).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = android.view.Gravity.CENTER_VERTICAL
            setPadding(dp(ctx, 2), dp(ctx, 9), dp(ctx, 2), dp(ctx, 9))
            addView(TextView(ctx).apply {
                this.text = text
                textSize = 15f
                setTextColor(TEXT)
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            })
            addView(sw)
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
            )
        }
        return SwitchRow(row, sw)
    }

    /** 行间分隔线：与值列左对齐缩进。 */
    fun divider(ctx: Context): View = View(ctx).apply {
        setBackgroundColor(0xFFEDF0F0.toInt())
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, 1
        ).apply { marginStart = dp(ctx, 86) }
    }

    /** 状态行：左标签 + 右值（纯信息展示，不可点），返回行与值控件供动态刷新。 */
    fun statusRow(ctx: Context, label: String): Row {
        val valueView = TextView(ctx).apply {
            text = "…"
            textSize = 14.5f
            setTextColor(TEXT)
            gravity = android.view.Gravity.END
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        val row = LinearLayout(ctx).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = android.view.Gravity.CENTER_VERTICAL
            setPadding(dp(ctx, 2), dp(ctx, 11), dp(ctx, 2), dp(ctx, 11))
            addView(TextView(ctx).apply {
                text = label
                textSize = 14f
                setTextColor(SUBTEXT)
                layoutParams = LinearLayout.LayoutParams(dp(ctx, 84), ViewGroup.LayoutParams.WRAP_CONTENT)
            })
            addView(valueView)
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
            )
        }
        return Row(row, valueView)
    }

    /** 功能卡头：左侧标题（粗体）+ 右侧快捷开关。返回容器与开关。 */
    fun cardHeader(ctx: Context, title: String, sw: Switch): LinearLayout =
        LinearLayout(ctx).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = android.view.Gravity.CENTER_VERTICAL
            setPadding(0, dp(ctx, 2), 0, dp(ctx, 6))
            addView(TextView(ctx).apply {
                text = title
                textSize = 16.5f
                setTextColor(TEXT)
                typeface = android.graphics.Typeface.DEFAULT_BOLD
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            })
            addView(sw)
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
            )
        }

    fun pageRoot(ctx: Context): LinearLayout = LinearLayout(ctx).apply {
        orientation = LinearLayout.VERTICAL
        setBackgroundColor(BG)
        setPadding(dp(ctx, 16), dp(ctx, 16), dp(ctx, 16), dp(ctx, 16))
    }

    /** 全面屏适配：targetSdk 35+ 强制边到边，内容会钻到状态栏/挖孔下面。
     *  给内容根视图挂 Insets 监听，自动留出状态栏与导航栏高度。 */
    fun fitSystemBars(view: android.view.View) {
        view.setOnApplyWindowInsetsListener { v, insets ->
            val bars = insets.getInsets(android.view.WindowInsets.Type.systemBars())
            v.setPadding(v.paddingLeft, bars.top, v.paddingRight, bars.bottom)
            insets
        }
    }
}

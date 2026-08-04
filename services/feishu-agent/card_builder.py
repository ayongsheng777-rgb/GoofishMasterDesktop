# -*- coding: utf-8 -*-
"""Card Builder — Build Feishu interactive cards for AI analysis results."""
from __future__ import annotations
from typing import Any, Dict, List, Optional


def build_product_card(product: Dict[str, Any],
                       analysis: Optional[Dict[str, Any]] = None) -> dict:
    """Build a product analysis card.

    product: {title, price, url, image, seller_name, ...}
    analysis: {seller_type, seller_score, risk_score, risk_level, recommend, reasons, final_score}
    """
    title = product.get("title", "未知商品")
    price = product.get("price", "未知")
    url = product.get("url", "")
    seller_name = product.get("seller_name", "未知")

    elements = []

    # Product info section
    info_lines = [f"**📦 {title}**", f"💰 价格: ¥{price}"]
    if product.get("market_price"):
        market = product["market_price"]
        discount = product.get("discount_rate", 0)
        info_lines.append(f"📊 市场价: ¥{market} (优惠 {discount:.0f}%)")
    info_lines.append(f"👤 卖家: {seller_name}")

    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": "\n".join(info_lines)}
    })

    if analysis:
        elements.append({"tag": "hr"})

        # Analysis scores
        score_lines = []
        seller_type = analysis.get("seller_type", "未知")
        seller_score = analysis.get("seller_score", 0)
        risk_score = analysis.get("risk_score", 0)
        risk_level = analysis.get("risk_level", "未知")
        final_score = analysis.get("final_score", 0)

        # Seller type badge
        seller_emoji = "✅" if seller_type == "个人卖家" else "⚠️"
        score_lines.append(f"{seller_emoji} 卖家类型: {seller_type} (可信度 {seller_score}分)")

        # Risk level
        risk_emoji = {"低": "🟢", "中": "🟡", "高": "🔴"}.get(risk_level, "⚪")
        score_lines.append(f"{risk_emoji} 风险等级: {risk_level} ({risk_score}分)")

        # Final score with stars
        stars = _score_to_stars(final_score)
        score_lines.append(f"⭐ AI评分: {final_score}分 {stars}")

        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(score_lines)}
        })

        # Reasons
        reasons = analysis.get("reasons", [])
        if reasons:
            elements.append({"tag": "hr"})
            reason_text = "**📋 分析理由**\n" + "\n".join(f"• {r}" for r in reasons[:5])
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": reason_text}
            })

        # Recommendation
        recommend = analysis.get("recommend", False)
        rec_text = "✅ 推荐购买" if recommend else "❌ 不建议购买"
        rec_color = "green" if recommend else "red"
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**{rec_text}**"}
        })

    # Action buttons
    actions = []
    if url:
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🔗 查看商品"},
            "type": "primary",
            "url": url
        })

    if actions:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "action",
            "actions": actions
        })

    # Determine header color based on analysis
    header_color = "blue"
    if analysis:
        if analysis.get("recommend"):
            header_color = "green"
        elif analysis.get("risk_level") == "高":
            header_color = "red"
        elif analysis.get("risk_level") == "中":
            header_color = "yellow"

    header_title = f"🎯 AI发现商品"
    if analysis and analysis.get("final_score", 0) >= 80:
        header_title = f"🌟 AI推荐商品 ({analysis['final_score']}分)"

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": header_color,
            "title": {"tag": "plain_text", "content": header_title}
        },
        "elements": elements
    }


def build_search_results_card(results: List[Dict[str, Any]],
                               keyword: str) -> dict:
    """Build a search results summary card."""
    elements = []

    if not results:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"未找到 \"{keyword}\" 相关商品"}
        })
    else:
        lines = [f"**🔍 搜索 \"{keyword}\" 结果**", f"共找到 {len(results)} 个商品\n"]
        for i, item in enumerate(results[:10], 1):
            score = item.get("final_score", 0)
            score_emoji = "🌟" if score >= 80 else "✅" if score >= 60 else "⚪"
            price = item.get("price", "?")
            title = item.get("title", "未知")[:30]
            lines.append(f"{score_emoji} **{i}.** {title}")
            lines.append(f"   💰 ¥{price} | AI评分: {score}分")

        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(lines)}
        })

        if len(results) > 10:
            elements.append({
                "tag": "note",
                "elements": [{"tag": "plain_text",
                              "content": f"还有 {len(results)-10} 个结果，发送「分析第N个」查看详情"}]
            })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": f"🔍 搜索: {keyword}"}
        },
        "elements": elements
    }


def build_task_card(task: Dict[str, Any]) -> dict:
    """Build a monitoring task status card."""
    status_emoji = {"running": "🟢", "stopped": "🔴", "paused": "🟡"}.get(
        task.get("status", ""), "⚪")

    lines = [
        f"**📋 {task.get('name', '未命名任务')}**",
        f"关键词: {task.get('keyword', '未知')}",
        f"状态: {status_emoji} {task.get('status', '未知')}",
    ]
    if task.get("max_price"):
        lines.append(f"价格上限: ¥{task['max_price']}")
    if task.get("interval"):
        lines.append(f"监控间隔: {task['interval']}分钟")
    if task.get("found_count"):
        lines.append(f"已发现商品: {task['found_count']}个")

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "📋 监控任务"}
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}
        ]
    }


def build_status_card(status: Dict[str, Any]) -> dict:
    """Build system status card."""
    lines = [
        "**🖥️ 系统状态**",
        f"运行时间: {status.get('uptime', '未知')}",
        f"活跃任务: {status.get('active_tasks', 0)}",
        f"今日发现: {status.get('today_found', 0)}",
        f"AI调用次数: {status.get('ai_calls', 0)}",
    ]

    services = status.get("services", {})
    if services:
        lines.append("\n**📡 服务状态**")
        for name, state in services.items():
            emoji = "🟢" if state == "running" else "🔴"
            lines.append(f"{emoji} {name}: {state}")

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "🖥️ 系统状态"}
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}
        ]
    }


def _score_to_stars(score: int) -> str:
    """Convert score to star rating."""
    if score >= 90:
        return "⭐⭐⭐⭐⭐"
    elif score >= 75:
        return "⭐⭐⭐⭐"
    elif score >= 60:
        return "⭐⭐⭐"
    elif score >= 40:
        return "⭐⭐"
    else:
        return "⭐"

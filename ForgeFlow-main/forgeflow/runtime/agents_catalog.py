"""Platform agent catalog — the 6 product-level agent roles (docs §1.1).

This is the backend source of truth for the "我的 Agent" home-page section, so
the frontend renders real API data instead of a hardcoded array.
"""

from __future__ import annotations

PLATFORM_AGENTS: list[dict[str, object]] = [
    {
        "agent_id": "planner",
        "name": "任务规划师",
        "category": "Planner Agent",
        "description": "任务拆解、流程规划、资源分配",
        "status": "在线",
        "color": "blue",
    },
    {
        "agent_id": "data",
        "name": "数据分析师",
        "category": "Data Agent",
        "description": "数据查询、分析、可视化",
        "status": "在线",
        "color": "emerald",
    },
    {
        "agent_id": "code",
        "name": "代码开发师",
        "category": "Code Agent",
        "description": "代码编辑、调试、测试、修复",
        "status": "在线",
        "color": "purple",
    },
    {
        "agent_id": "research",
        "name": "研究助手",
        "category": "Research Agent",
        "description": "信息检索、资料整理、深度分析",
        "status": "在线",
        "color": "amber",
    },
    {
        "agent_id": "document",
        "name": "文档处理师",
        "category": "Document Agent",
        "description": "文档解析、知识提取、总结",
        "status": "在线",
        "color": "blue",
    },
    {
        "agent_id": "security",
        "name": "安全审计师",
        "category": "Security Agent",
        "description": "权限控制、风险检测、合规审计",
        "status": "在线",
        "color": "emerald",
    },
]

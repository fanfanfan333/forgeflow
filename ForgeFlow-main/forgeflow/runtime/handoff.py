"""Validation for untrusted results supplied to a code run."""

import json


def validate_handoff_results(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise ValueError("前置结果必须包含 1–8 条资料")
    for item in value:
        if not isinstance(item, dict) or set(item) - {"kind", "source", "content", "truncated"}:
            raise ValueError("前置结果格式无效")
        if item.get("kind") not in {"document", "research"}:
            raise ValueError("前置结果类型无效")
        for key, limit in (("source", 512), ("content", 12000)):
            text = item.get(key)
            if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > limit:
                raise ValueError(f"前置结果 {key} 无效或超长")
        if "truncated" in item and not isinstance(item["truncated"], bool):
            raise ValueError("前置结果截断标志无效")
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > 32768:
        raise ValueError("前置结果总大小超限")
    return value

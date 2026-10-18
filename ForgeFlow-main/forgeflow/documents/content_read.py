"""Bounded, located DOCX excerpts for real paragraph and table reading."""

from __future__ import annotations

import re
from typing import Any

from docx.table import Table

from forgeflow.documents.docx_inspect import open_docx


def read_content(
    data: bytes,
    query: str = "",
    offset: int = 0,
    limit: int = 12,
    *,
    char_offset: int = 0,
    task_id: str = "",
    field: str = "",
) -> dict[str, Any]:
    if bool(task_id) != bool(field) or (
        task_id
        and (
            not re.fullmatch(r"[A-Za-z]{1,8}[-_]?\d{1,8}", task_id)
            or not 1 <= len(field.strip()) <= 80
        )
    ):
        raise ValueError("task_id and field must be supplied together with a valid task identifier")
    document = open_docx(data)
    records: list[tuple[str, str, str]] = []
    heading = "文档开头"
    paragraph_index = table_index = 0
    for block in document.iter_inner_content():
        if isinstance(block, Table):
            cells = [cell.text.strip() for cell in block.rows[0].cells] if block.rows else []
            caption = (
                cells[0]
                if cells and all(not value or value == cells[0] for value in cells[1:])
                else ""
            )
            table_heading = f"{caption[:100]} / {heading}" if caption else heading
            for row_index, row in enumerate(block.rows):
                text = " | ".join(c.text for c in row.cells)
                records.append((f"table:{table_index}/row:{row_index}", table_heading, text))
            table_index += 1
        else:
            if (block.style and block.style.name.startswith("Heading")) or re.fullmatch(
                r"[A-Za-z]{1,8}[-_]?\d{1,8}", block.text.strip()
            ):
                heading = block.text.strip()
            if block.text.strip():
                records.append((f"paragraph:{paragraph_index}", heading, block.text))
            paragraph_index += 1
    terms = set(re.findall(r"\b[A-Za-z][A-Za-z0-9_]{1,}\b", query))
    terms.update(re.findall(r"「([^「」]+)」", query))
    chinese = re.findall(r"[\u4e00-\u9fff]{2,}", query)
    terms.update(word[i : i + 2] for word in chinese for i in range(len(word) - 1))
    terms.difference_update(
        {
            "任务",
            "文档",
            "指令",
            "文件",
            "执行",
            "读取",
            "上传",
            "依据",
            "回答",
            "全部",
            "问题",
            "说明",
            "来源",
            "表名",
            "什么",
            "是否",
            "能否",
            "不要",
            "修改",
            "其他",
            "历史",
            "知识",
            "找不",
            "不到",
            "明确",
            "无法",
            "确定",
            "猜测",
        }
    )

    def identifiers(text: str) -> set[str]:
        return set(
            re.findall(
                r"(?<![A-Za-z0-9_])[A-Za-z]{1,8}[-_]?\d{1,8}(?![A-Za-z0-9_])", text.casefold()
            )
        )

    def score(
        record: tuple[str, str, str],
        keywords: set[str] | None = None,
        question: str | None = None,
    ) -> float:
        keywords = terms if keywords is None else keywords
        question = query if question is None else question
        _, title, text = record
        body = text.casefold()
        first = text.split(" | ")[0].strip()
        points: float = float(
            sum(
                (8 if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]+", term) else 2)
                if term.casefold() in body
                else 0
                for term in keywords
            )
        )
        points += sum(0.3 for term in keywords if term.casefold() in title.casefold())
        points += sum(12 for term in keywords if term.casefold() == first.casefold())
        if 3 <= len(first) <= 24 and first.casefold() in question.casefold():
            points += 12
        if (
            "迁移" in question
            and "迁移" in title
            and any(re.fullmatch(r"T\d+", term) and term in text for term in keywords)
        ):
            points += 12
        # Entity and requested field outrank short incidental mentions of an ID.
        entity_match = bool(identifiers(title.split(" / ")[0]) & identifiers(question))
        field_match = (
            2 <= len(first) <= 24
            and not identifiers(first)
            and first.casefold() in question.casefold()
        )
        return points / (1 + len(text) / 200) + 80 * entity_match + 40 * field_match

    if task_id:
        records = [
            record
            for record in records
            if task_id.casefold() in identifiers(record[1].split(" / ")[0])
            and record[2].split(" | ")[0].strip() == field.strip()
        ]
    if query.strip():
        records.sort(key=score, reverse=True)
        questions = re.split(r"(?<!\w)\d+[.、]\s*", query)[1:]
        if 2 <= len(questions) <= 12:
            # A single global rank can crowd out the final question. Reserve
            # evidence for each numbered requirement before filling the page.
            rankings = []
            for question in questions:
                keywords = {t for t in terms if t.casefold() in question.casefold()}

                def question_score(
                    record: tuple[str, str, str],
                    keywords: set[str] = keywords,
                    question: str = question,
                ) -> float:
                    return score(record, keywords, question)

                rankings.append(sorted(records, key=question_score, reverse=True))
            prioritized = []
            seen = set()
            for depth in range(3):
                for ranking in rankings:
                    for record in ranking[depth:]:
                        if record[0] not in seen:
                            prioritized.append(record)
                            seen.add(record[0])
                            break
            records = prioritized + [r for r in records if r[0] not in seen]
    offset = max(0, offset)
    limit = max(1, min(limit, 20))
    selected: list[str] = []
    used = 0
    consumed = 0
    next_char_offset = 0
    for location, title, text in records[offset : offset + limit]:
        prefix = f"[{location}] {title[:120]}: "
        start = max(0, char_offset) if consumed == 0 else 0
        text = text[start:]
        available = 2200 - used
        if available < len(prefix) + 40:
            break
        count = available - len(prefix)
        line = prefix + text[:count]
        selected.append(line)
        used += len(line) + 1
        if len(text) > count:
            next_char_offset = start + count
            break
        consumed += 1
    next_offset = offset + consumed
    return {
        "text": "\n".join(selected),
        "total_records": len(records),
        "next_offset": next_offset if next_offset < len(records) else None,
        "next_char_offset": next_char_offset,
        "truncated": next_offset < len(records),
        "selection": {"task_id": task_id, "field": field, "matches": len(records)}
        if task_id
        else None,
    }

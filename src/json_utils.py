"""Shared tolerant JSON extraction helpers (strips <think> blocks and code fences)."""

import re
from typing import Iterator


def json_candidates(text: str) -> Iterator[str]:
    """Yield candidate JSON object strings from LLM text, tolerating <think> and code fences."""
    cleaned = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    yield cleaned
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.S):
        yield m.group(1)
    # last balanced {...} block
    depth, start = 0, None
    blocks: list[str] = []
    for i, ch in enumerate(cleaned):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                blocks.append(cleaned[start : i + 1])
    yield from reversed(blocks)


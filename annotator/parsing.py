"""Parsing and validation of import payloads.

Only integer pixel coordinates are accepted.  JSON numbers such as ``3.0``
or strings such as ``"3"`` are rejected rather than silently coerced, so a
typo cannot silently shift a box by a pixel.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .geometry import Rect

MAX_TILES = 20


@dataclass
class ParsedImport:
    cracks: dict[int, Rect]
    tiles: dict[int, Rect]


class ImportPayloadError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _read_box(entry: dict, where: str, errors: list[str]) -> tuple[int, Rect] | None:
    number = entry.get("id")
    if not _is_int(number) or number <= 0:
        errors.append(f"{where}: id 必须是正整数，收到 {number!r}。")
        return None
    values = []
    for field in ("x", "y", "w", "h"):
        value = entry.get(field)
        if not _is_int(value):
            errors.append(f"{where} (id={number}): {field} 必须是整数像素，收到 {value!r}。")
            return None
        values.append(value)
    x, y, w, h = values
    if w <= 0 or h <= 0:
        errors.append(f"{where} (id={number}): 宽和高必须为正，收到 w={w}, h={h}。")
        return None
    return number, Rect(x, y, w, h)


def _read_group(
    payload: dict, key: str, label: str, errors: list[str]
) -> dict[int, Rect]:
    raw = payload.get(key)
    if raw is None:
        errors.append(f"缺少字段 {key!r}（{label}）。")
        return {}
    if not isinstance(raw, list):
        errors.append(f"{key!r} 必须是数组（{label}）。")
        return {}
    result: dict[int, Rect] = {}
    for index, entry in enumerate(raw):
        where = f"{key}[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{where}: 必须是包含 id/x/y/w/h 的对象。")
            continue
        parsed = _read_box(entry, where, errors)
        if parsed is None:
            continue
        number, rect = parsed
        if number in result:
            errors.append(f"{key}: id={number} 重复。")
            continue
        result[number] = rect
    return result


def parse_import_text(text: str) -> ParsedImport:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ImportPayloadError([f"JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}。"])
    if not isinstance(payload, dict):
        raise ImportPayloadError(["导入内容必须是一个 JSON 对象。"])

    errors: list[str] = []
    cracks = _read_group(payload, "cracks", "裂缝框", errors)
    tiles = _read_group(payload, "tiles", "切片框", errors)

    if not cracks:
        errors.append("至少需要一个裂缝框。")
    if not tiles:
        errors.append("至少需要一个切片框。")
    if len(tiles) > MAX_TILES:
        errors.append(f"切片框最多 {MAX_TILES} 个，收到 {len(tiles)} 个。")
    if errors:
        raise ImportPayloadError(errors)
    return ParsedImport(cracks=cracks, tiles=tiles)

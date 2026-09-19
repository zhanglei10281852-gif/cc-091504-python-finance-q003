"""JSON 编解码:支持 Decimal / date / datetime / Enum / dataclass。

用于 .runtime 持久化与事件日志。序列化结果可逆,加载后对象图与
保存前一致(tuple 字段由各 dataclass 的 __post_init__ 归一化)。
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, TypeVar

_REGISTRY: dict[str, type] = {}

T = TypeVar("T")


def register(cls: type[T]) -> type[T]:
    _REGISTRY[cls.__name__] = cls
    return cls


def to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, bool):
        return obj
    if isinstance(obj, Enum):
        # 注意:str 子类枚举必须先于 str 判断,否则丢失枚举类型
        return {"$enum": f"{type(obj).__name__}:{obj.value}"}
    if isinstance(obj, (str, int, float)):
        return obj
    if isinstance(obj, Decimal):
        return {"$decimal": str(obj)}
    if isinstance(obj, datetime):
        return {"$datetime": obj.isoformat()}
    if isinstance(obj, date):
        return {"$date": obj.isoformat()}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        payload = {"$type": type(obj).__name__}
        for f in dataclasses.fields(obj):
            payload[f.name] = to_jsonable(getattr(obj, f.name))
        return payload
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    raise TypeError(f"不可序列化的对象: {type(obj)!r}")


def from_jsonable(obj: Any) -> Any:
    if isinstance(obj, list):
        return [from_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        if "$decimal" in obj:
            return Decimal(obj["$decimal"])
        if "$datetime" in obj:
            return datetime.fromisoformat(obj["$datetime"])
        if "$date" in obj:
            return date.fromisoformat(obj["$date"])
        if "$enum" in obj:
            name, _, value = obj["$enum"].partition(":")
            return _REGISTRY[name](value)
        if "$type" in obj:
            cls = _REGISTRY[obj["$type"]]
            kwargs = {k: from_jsonable(v) for k, v in obj.items() if k != "$type"}
            return cls(**kwargs)
        return {k: from_jsonable(v) for k, v in obj.items()}
    return obj


def dumps(obj: Any) -> str:
    return json.dumps(to_jsonable(obj), ensure_ascii=False, sort_keys=True)


def loads(text: str) -> Any:
    return from_jsonable(json.loads(text))


def plain(obj: Any) -> Any:
    """纯 JSON 结构:枚举取值、Decimal/时间转字符串,供 API 响应与报表输出。"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [plain(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): plain(v) for k, v in obj.items()}
    return str(obj)

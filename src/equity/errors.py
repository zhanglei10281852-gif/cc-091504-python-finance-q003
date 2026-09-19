"""业务错误,携带稳定 code 供 API 映射与调用方判断。"""

from __future__ import annotations


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


class NotFoundError(ServiceError):
    def __init__(self, message: str):
        super().__init__("not_found", message, status=404)


class ConflictError(ServiceError):
    """状态冲突:如重复结算、倒改已结算批次。"""

    def __init__(self, message: str, code: str = "conflict"):
        super().__init__(code, message, status=409)


class ValidationError(ServiceError):
    def __init__(self, message: str, code: str = "validation"):
        super().__init__(code, message, status=400)

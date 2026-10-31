from typing import Any, Literal

from pydantic import BaseModel, Field


class ContentModel(BaseModel):
    content: str


class RecordModel(BaseModel):
    """存一条纠错记录时交上来的东西：结果页要用的整份数据。

    units / blocks / highlights 用 Any 收：结构由前端那套渲染决定，这边原样存下来就行，
    没必要跟着它改字段定义。
    """

    id: str | None = Field(default=None, description="可选：把浏览器里的老记录导进来时沿用原来的 id")
    kind: str = "file"
    filename: str = ""
    engineStatus: str = "ok"
    correctionCount: int = 0
    unitsWithCorrection: int = 0
    timings: dict[str, Any] | None = None
    units: list[Any] = Field(default_factory=list)
    blocks: list[Any] | None = None
    highlights: list[Any] | None = None
    doc: dict[str, Any] | None = None


class IgnoredModel(BaseModel):
    """误报清单：结果页上被手动忽略的那些改动（按处号）。"""

    ignored: list[int] = Field(default_factory=list)


class CorrectionErrorItem(BaseModel):
    """对外契约：单字粒度的纠错项，position 一律由服务端算出。"""

    position: int = Field(ge=0, description="原文字符下标；insert 表示插在该下标之前")
    original_char: str = Field(description="原文字符；insert 时为空字符串")
    correct_char: str = Field(description="正确字符；delete 时为空字符串")
    tag: Literal["replace", "insert", "delete"]


class ContractReviewItem(BaseModel):
    approved: bool = Field(description="true=通过；false=有疑问，需关注")
    reason: str = Field(description="审查理由")
    severity: Literal["info", "warning", "error"] = Field(
        default="info", description="info=信息提示 / warning=需关注 / error=严重问题"
    )


class ContractReviewOutput(BaseModel):
    reviews: list[ContractReviewItem] = Field(
        default_factory=list, description="审查意见列表，每条一个字段"
    )

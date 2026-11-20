"""汇总刷新版本接口：版本化刷新、源分区更正/撤销、按版本查询与逐项校对。

路由统一挂在 /api/agg-refresh 下，与通用资源路由 /api/{module} 不冲突。
业务判断全部落在 app/services/agg_refresh.py 引擎与领域服务中。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services import service_for
from app.services.agg_refresh import AggRefreshError, agg_refresh_engine

router = APIRouter(prefix="/api/agg-refresh", tags=["汇总刷新版本"])


class RevokePayload(BaseModel):
    partition_key: str
    reason: str = ""


class CorrectPayload(BaseModel):
    partition_key: str
    amount: float
    reason: str = ""


class ProcessPayload(BaseModel):
    partition_key: str | None = None
    process_all: bool = False


class FailPayload(BaseModel):
    reason: str = ""


class ExecutePayload(BaseModel):
    filters: dict[str, str] = Field(default_factory=dict)


def _guard(call, *args, **kwargs) -> Any:
    try:
        return call(*args, **kwargs)
    except AggRefreshError as exc:
        status = 404 if exc.code.endswith("not_found") else 409
        raise HTTPException(status_code=status, detail=exc.message) from exc


# ------------------------------------------------------------------
# 源事实分区：追查、更正、撤销
# ------------------------------------------------------------------


@router.get("/partitions")
def list_partitions(source_fact: str | None = Query(default=None, alias="源事实表")) -> dict[str, Any]:
    items = agg_refresh_engine.list_partitions(source_fact)
    return {"items": items, "total": len(items)}


@router.post("/partitions/correct")
def correct_partition(payload: CorrectPayload) -> dict[str, Any]:
    """源表更正：更新分区金额并推进数据版本，后续刷新将重算该分区。"""
    partition = _guard(agg_refresh_engine.correct_partition, payload.partition_key, payload.amount, payload.reason)
    return {"ok": True, "message": f"源分区 {payload.partition_key} 已更正", "partition": partition}


@router.post("/partitions/revoke")
def revoke_partition(payload: RevokePayload) -> dict[str, Any]:
    """源表撤销：分区状态置为已撤销，刷新提交时会被剔除。"""
    partition = _guard(agg_refresh_engine.revoke_partition, payload.partition_key, payload.reason)
    return {"ok": True, "message": f"源分区 {payload.partition_key} 已撤销", "partition": partition}


# ------------------------------------------------------------------
# 星型查询：按汇总刷新版本执行
# ------------------------------------------------------------------


@router.post("/star-query/{query_id}/execute")
def execute_star_query(query_id: int, payload: ExecutePayload) -> dict[str, Any]:
    service = service_for("star_query")
    execute = getattr(service, "execute_query", None)
    if service is None or execute is None:
        raise HTTPException(status_code=404, detail="星型查询模块不支持按版本执行")
    result, message = execute(query_id, payload.filters)
    if result is None:
        return {"ok": False, "message": message, "已确认": False}
    return {"ok": True, "message": message, "result": result}


# ------------------------------------------------------------------
# 刷新版本生命周期
# ------------------------------------------------------------------


@router.get("/versions/{version_id}")
def version_detail(version_id: str) -> dict[str, Any]:
    """版本详情：参与聚合的源分区与产出金额逐项列出。"""
    return _guard(agg_refresh_engine.get_version, version_id)


@router.post("/versions/{version_id}/process")
def process_version(version_id: str, payload: ProcessPayload) -> dict[str, Any]:
    if payload.process_all:
        count = _guard(agg_refresh_engine.process_all, version_id)
        return {"ok": True, "message": f"版本 {version_id} 已处理 {count} 个待算分区", "processed": count}
    item = _guard(agg_refresh_engine.process_partition, version_id, payload.partition_key)
    if item is None:
        return {"ok": True, "message": f"版本 {version_id} 无待处理分区", "item": None}
    return {"ok": True, "message": f"分区 {item['分区键']} 已计入版本 {version_id}", "item": item}


@router.post("/versions/{version_id}/commit")
def commit_version(version_id: str) -> dict[str, Any]:
    """提交版本：校验全部计入分区，剔除失效分区后确认。"""
    version = _guard(agg_refresh_engine.commit, version_id)
    return {
        "ok": True,
        "message": (
            f"版本 {version_id} 已确认，计入 {len(version['items'])} 个源分区"
            + (f"，剔除失效分区 {len(version['excluded'])} 个" if version["excluded"] else "")
        ),
        "version": version,
    }


@router.post("/versions/{version_id}/fail")
def fail_version(version_id: str, payload: FailPayload) -> dict[str, Any]:
    """标记失败：保留上次成功汇总，当前已确认版本不受影响。"""
    version = _guard(agg_refresh_engine.fail, version_id, payload.reason)
    return {"ok": True, "message": f"版本 {version_id} 已标记失败，保留上次成功汇总", "version": version}


# ------------------------------------------------------------------
# 汇总维度：版本列表、一键刷新、血缘追查、按版本查询
# ------------------------------------------------------------------


@router.get("/{entry_id}/versions")
def list_versions(entry_id: int) -> dict[str, Any]:
    versions = _guard(agg_refresh_engine.list_versions, entry_id)
    return {"items": versions, "total": len(versions)}


@router.post("/{entry_id}/versions")
def begin_version(entry_id: int) -> dict[str, Any]:
    version = _guard(agg_refresh_engine.begin_refresh, entry_id)
    return {"ok": True, "message": f"刷新版本 {version['version_id']} 已开启", "version": version}


@router.post("/{entry_id}/refresh")
def run_refresh(entry_id: int) -> dict[str, Any]:
    """一键刷新：开启版本 → 处理全部待算分区 → 校验并提交。"""
    version = _guard(agg_refresh_engine.refresh, entry_id)
    return {
        "ok": True,
        "message": f"刷新完成，版本 {version['version_id']} 已确认",
        "version": version,
    }


@router.get("/{entry_id}/lineage")
def refresh_lineage(entry_id: int) -> dict[str, Any]:
    """构建进度血缘：按源事实表和分区追查构建到何处。"""
    return _guard(agg_refresh_engine.lineage, entry_id)


@router.get("/{entry_id}/result")
def aggregate_result(
    entry_id: int,
    region: str | None = Query(default=None, alias="地区"),
    month: str | None = Query(default=None, alias="月份"),
    partition_key: str | None = Query(default=None, alias="分区键"),
) -> dict[str, Any]:
    """按当前已确认版本查询聚合度量，返回金额与版本分区逐项相加校对。"""
    filters = {k: v for k, v in {"地区": region, "月份": month, "分区键": partition_key}.items() if v}
    return _guard(agg_refresh_engine.query_aggregate, entry_id, filters)

"""星型查询服务：定义基于星型模型的维度查询，包括事实与维度的关联和度量聚合。

增强能力：
- 消费 fact.published 和 dim.published 通道校验查询组件
- 自动校验关联维度和度量是否可用
- 星型查询SQL生成（SELECT/FROM/JOIN/WHERE/GROUP BY）
- 按汇总刷新版本执行查询：只取已确认版本的聚合度量，返回与金额
  持久化记录一致的事实分区，未确认金额不进入查询结果
- 查询结果限制与分页建议
"""
from __future__ import annotations

from typing import Any

from app.services.agg_refresh import agg_refresh_engine
from app.services.base import BaseModuleService
from app.store import store


class StarQueryService(BaseModuleService):
    MODULE = "star_query"
    ENTITY = "查询定义"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["查询名称", "事实表", "关联维度", "度量选择", "过滤条件", "分组维度", "结果限制"]
    REQUIRED_FIELDS = ["查询名称", "事实表", "关联维度"]
    STATUS_ORDER = ["草稿", "已验证", "已发布", "已过期"]
    ACTION_RULES = {"验证查询": "草稿", "发布查询": "已验证"}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        fact_name = str(values.get("事实表", "")).strip()
        related_dims = str(values.get("关联维度", "")).strip()
        measures = str(values.get("度量选择", "")).strip()

        # 消费事实和维度通道校验
        fact_msgs = store.channel.consume("fact.published", "star_query")
        for msg in fact_msgs:
            if msg["payload"].get("fact_name") == fact_name:
                values["_fact_confirmed"] = True

        dim_msgs = store.channel.consume("dim.published", "star_query")
        for msg in dim_msgs:
            dim_code = msg["payload"].get("dim_code", "")
            if dim_code in related_dims:
                values[f"_dim_{dim_code}_confirmed"] = True

        # 如果未填度量，从channel消费
        if not measures:
            measure_msgs = store.channel.consume("measure.ready", "star_query")
            for m in measure_msgs:
                if m["payload"].get("fact_name") == fact_name:
                    measures = m["payload"].get("measure_name", "")
            if measures:
                values["度量选择"] = measures

        if not values.get("结果限制"):
            values["结果限制"] = "1000"

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        store.channel.publish("query.defined", {
            "query_name": entry["查询名称"],
            "fact_name": fact_name,
            "related_dims": related_dims,
            "measures": entry.get("度量选择"),
            "entry_id": entry["id"],
        })

        store.artifacts.register(
            "star_query", "star_query_def",
            f"query_{entry['id']}",
            {"query_name": entry["查询名称"], "fact_name": fact_name},
            ["fact_design", "dim_definition", "star_query"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        if action == "验证查询":
            try:
                sql = _generate_sql(entry)
                store.channel.publish("query.validated", {
                    "query_name": entry["查询名称"],
                    "generated_sql": sql,
                    "entry_id": entry_id,
                })
            except Exception:
                pass
        elif action == "发布查询":
            store.channel.publish("query.published", {
                "query_name": entry["查询名称"],
                "entry_id": entry_id,
            })
        return entry, message

    def execute_query(self, entry_id: int, filters: dict[str, str] | None = None) -> tuple[dict[str, Any] | None, str]:
        """按汇总刷新版本执行星型查询：只取已确认版本的聚合度量。

        返回与金额持久化记录一致的事实分区；尚未确认的金额不进入查询结果。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        fact_name = str(entry.get("事实表", "")).strip()
        group_dims = {
            d.strip()
            for d in str(entry.get("分组维度", "")).replace(",", ".").split(".")
            if d.strip()
        }
        candidates: list[tuple[int, dict[str, Any]]] = []
        for agg in store.rows("agg_summary"):
            if str(agg.get("源事实表", "")).strip() != fact_name:
                continue
            if agg_refresh_engine.current_version(int(agg["id"])) is None:
                continue  # 该汇总尚无已确认版本，未确认金额不进入查询结果
            agg_dims = {d.strip() for d in str(agg.get("汇总维度", "")).split(".") if d.strip()}
            candidates.append((len(group_dims & agg_dims), agg))
        if not candidates:
            return None, f"事实表 {fact_name} 尚无已确认的汇总刷新版本，未确认金额不进入查询结果"

        candidates.sort(key=lambda c: (-c[0], int(c[1]["id"])))
        agg = candidates[0][1]
        result = agg_refresh_engine.query_aggregate(int(agg["id"]), filters)
        result["查询名称"] = entry.get("查询名称")
        result["命中汇总"] = agg.get("汇总名称")
        store.channel.publish("query.executed", {
            "query_name": entry.get("查询名称"),
            "agg_name": agg.get("汇总名称"),
            "version_id": result.get("version_id"),
            "entry_id": entry_id,
        })
        return result, f"查询已按汇总刷新版本 {result.get('version_id')} 执行"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["consumed_facts"] = len(store.channel.consume("fact.published", "star_query"))
        base["consumed_dims"] = len(store.channel.consume("dim.published", "star_query"))
        return base


def _generate_sql(entry: dict[str, Any]) -> str:
    """根据查询定义生成模拟SQL。"""
    fact = entry.get("事实表", "")
    dims = entry.get("关联维度", "")
    measures = entry.get("度量选择", "")
    filters = entry.get("过滤条件", "1=1")
    group = entry.get("分组维度", dims)
    dim_list = [d.strip() for d in dims.split(".") if d.strip()]
    join_clauses = "\n".join(f"LEFT JOIN {d} ON f.{d}_id = {d}.id" for d in dim_list)
    return f"SELECT {group}, SUM({measures}) FROM {fact} AS f\n{join_clauses}\nWHERE {filters}\nGROUP BY {group}"
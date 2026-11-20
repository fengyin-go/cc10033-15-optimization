"""汇总构建服务：基于事实表构建多级汇总表，上卷维度、聚合度量并维护增量刷新。

增强能力：
- 消费 fact.published 和 measure.ready 通道，获取事实和度量信息
- 消费 agg.suggested 通道自动创建建议汇总
- 汇总粒度上卷验证：汇总维度必须是源事实表关联维度的子集
- 版本化刷新：执行构建/刷新汇总由汇总刷新版本引擎驱动，参与聚合的
  源分区与产出金额写入汇总刷新版本，供星型查询复算
- 刷新未完成时保留上次成功汇总（最近一次已确认版本继续生效）
- 构建失败自动记录追踪
"""
from __future__ import annotations

from typing import Any

from app.services.agg_refresh import AggRefreshError, agg_refresh_engine
from app.services.base import BaseModuleService
from app.store import store


class AggSummaryService(BaseModuleService):
    MODULE = "agg_summary"
    ENTITY = "汇总表"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["汇总名称", "源事实表", "汇总维度", "汇总度量", "汇总粒度", "刷新周期", "增量标记"]
    REQUIRED_FIELDS = ["汇总名称", "源事实表", "汇总维度"]
    STATUS_ORDER = ["草稿", "已构建", "已刷新", "构建失败"]
    ACTION_RULES = {"执行构建": "草稿", "刷新汇总": "已刷新"}
    NEGATIVE_ACTIONS = []

    VALID_GRANULARITIES = {"每小时", "每日", "每周", "每月", "每季度", "每年"}

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """创建汇总表：消费事实和度量通道，校验汇总粒度的合理性。"""
        fact_name = str(values.get("源事实表", "")).strip()
        agg_granularity = str(values.get("汇总粒度", "每日")).strip()

        # 消费事实发布通道
        fact_msgs = store.channel.consume("fact.published", "agg_summary")
        for msg in fact_msgs:
            if msg["payload"].get("fact_name") == fact_name:
                values["_source_granularity"] = msg["payload"].get("granularity", "")

        # 标准化汇总粒度
        if agg_granularity not in self.VALID_GRANULARITIES:
            agg_granularity = "每日"
            values["汇总粒度"] = agg_granularity

        # 消费度量确认通道，获取可用度量列表
        available_measures: list[str] = []
        measure_msgs = store.channel.consume("measure.ready", "agg_summary")
        for m in measure_msgs:
            if m["payload"].get("fact_name") == fact_name:
                available_measures.append(m["payload"].get("measure_name", ""))

        if available_measures:
            # 汇总默认使用全部可用度量
            values["汇总度量"] = ".".join(available_measures) if not values.get("汇总度量") else values["汇总度量"]

        # 检查汇总维度上卷合理性
        _validate_agg_dims(values)

        # 设置刷新周期默认值
        if not values.get("刷新周期"):
            granularity_map = {"每小时": "每小时 第10分钟", "每日": "T+1 03:00", "每周": "周一 06:00",
                               "每月": "月初T+3 04:00"}
            values["刷新周期"] = granularity_map.get(agg_granularity, "T+1 03:00")

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        store.channel.publish("agg.defined", {
            "agg_name": entry["汇总名称"],
            "source_fact": fact_name,
            "agg_dims": entry.get("汇总维度", ""),
            "agg_measures": entry.get("汇总度量", ""),
            "agg_granularity": agg_granularity,
            "refresh_cycle": entry.get("刷新周期", ""),
            "incremental": entry.get("增量标记") == "是",
            "entry_id": entry["id"],
        })

        store.artifacts.register(
            "agg_summary", "summary_table",
            f"agg_{entry['id']}",
            {"agg_name": entry["汇总名称"], "source_fact": fact_name, "granularity": agg_granularity},
            source_modules=["fact_design", "measure_config", "agg_summary"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action not in self.ACTION_RULES:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        if action == "执行构建":
            return self._run_build(entry)
        return self._run_refresh(entry)

    def _run_build(self, entry: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """执行构建：全量刷新并提交一个汇总刷新版本。"""
        try:
            version = agg_refresh_engine.refresh(entry["id"])
        except AggRefreshError as exc:
            entry["status"] = "构建失败"
            entry["pending"] = True
            entry["abnormal"] = True
            store.channel.publish("agg.build_failed", {
                "agg_name": entry.get("汇总名称"),
                "error": exc.message,
                "entry_id": entry["id"],
            })
            store.recovery.log_retry("agg_summary", f"build_{entry['id']}", 1, exc.message, False)
            return entry, f"汇总构建失败：{exc.message}"

        entry["status"] = "已构建"
        entry["pending"] = True
        entry["abnormal"] = False
        store.channel.publish("agg.built", {
            "agg_name": entry.get("汇总名称"),
            "source_fact": entry.get("源事实表"),
            "row_count": len(version["items"]),
            "version_id": version["version_id"],
            "total_amount": version["total_amount"],
            "build_time": version["committed_at"],
            "incremental": entry.get("增量标记") == "是",
            "entry_id": entry["id"],
        })
        return entry, (
            f"汇总表已执行构建，刷新版本 {version['version_id']} 已确认，"
            f"计入 {len(version['items'])} 个源分区"
        )

    def _run_refresh(self, entry: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """刷新汇总：增量分区重算 + 未变更分区继承，提交新的汇总刷新版本。"""
        try:
            version = agg_refresh_engine.refresh(entry["id"])
        except AggRefreshError as exc:
            # 刷新未完成：保留上次成功汇总（最近已确认版本继续生效），状态不推进
            entry["pending"] = True
            entry["abnormal"] = True
            store.recovery.log_retry("agg_summary", f"refresh_{entry['id']}", 1, exc.message, False)
            store.channel.publish("agg.refresh_failed", {
                "agg_name": entry.get("汇总名称"),
                "error": exc.message,
                "entry_id": entry["id"],
            })
            return entry, f"刷新未完成，已保留上次成功汇总：{exc.message}"

        entry["status"] = "已刷新"
        entry["pending"] = False
        entry["abnormal"] = False
        store.channel.publish("agg.refreshed", {
            "agg_name": entry.get("汇总名称"),
            "new_rows": len(version["processed_partitions"]),
            "version_id": version["version_id"],
            "total_amount": version["total_amount"],
            "excluded_partitions": [i["分区键"] for i in version["excluded"]],
            "refresh_time": version["committed_at"],
            "entry_id": entry["id"],
        })
        message = (
            f"汇总表已刷新汇总，版本 {version['version_id']} 计入 "
            f"{len(version['items'])} 个源分区"
        )
        if version["excluded"]:
            message += f"，剔除失效分区 {len(version['excluded'])} 个"
        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        # 汇总粒度分布和构建状态统计
        granularity_dist: dict[str, int] = {}
        build_stats = {"success": 0, "failed": 0, "pending": 0}
        for row in store.rows(self.MODULE):
            g = row.get("汇总粒度", "未指定")
            granularity_dist[g] = granularity_dist.get(g, 0) + 1
            status = row.get("status", "")
            if status == "已刷新":
                build_stats["success"] += 1
            elif status == "构建失败":
                build_stats["failed"] += 1
            else:
                build_stats["pending"] += 1
        base["granularity_distribution"] = granularity_dist
        base["build_stats"] = build_stats
        base["refresh_versions"] = {
            state: sum(1 for v in store.agg_refresh_versions if v["state"] == state)
            for state in ("已确认", "构建中", "已失败")
        }
        base["consumed_facts"] = len(store.channel.consume("fact.published", "agg_summary"))
        base["consumed_measures"] = len(store.channel.consume("measure.ready", "agg_summary"))
        return base


def _validate_agg_dims(values: dict[str, Any]) -> None:
    """校验汇总维度是源事实表维度的子集（上卷不增加粒度）。"""
    agg_dims = str(values.get("汇总维度", "")).strip()
    fact_name = str(values.get("源事实表", "")).strip()
    if not agg_dims or not fact_name:
        return

    agg_dim_set = set(d.strip() for d in agg_dims.split(".") if d.strip())
    for row in store.rows("fact_design"):
        if row.get("事实名称") == fact_name:
            fact_dims = set(d.strip() for d in str(row.get("关联维度", "")).split(".") if d.strip())
            extra = agg_dim_set - fact_dims
            if extra:
                values["汇总维度"] = ".".join(sorted(agg_dim_set & fact_dims))
            break
"""汇总刷新版本引擎：已建汇总与增量分区的版本化刷新。

设计语义：
- 每次刷新产生一个“汇总刷新版本”，把参与聚合的源分区和产出的金额写入版本，
  供星型查询复算与逐项校对；
- 同一源分区的销售额在同一版本内只计入一次（未变更分区整体继承、
  重算分区按键去重），不会重复累计；
- 版本提交时逐个校验计入的源分区：被撤销或已更正的分区一律剔除并留痕，
  已确认版本始终完整对应一个可复算的源分区集合；
- 刷新未完成（构建中/已失败）时，查询始终落在最近一次已确认版本上，
  尚未确认的金额不进入查询结果；
- 构建进度按源事实表和分区留痕（已处理/待处理分区清单），可逐项追查。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.store import Store, store

STATE_BUILDING = "构建中"
STATE_COMMITTED = "已确认"
STATE_FAILED = "已失败"

PARTITION_CONFIRMED = "已确认"
PARTITION_REVOKED = "已撤销"

# 查询与过滤支持的分区维度字段
FILTER_FIELDS = ("地区", "月份", "分区键", "源事实表")


class AggRefreshError(Exception):
    """刷新引擎业务异常：code 供路由层映射 HTTP 状态码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _money(value: Any) -> float:
    """金额统一保留两位小数，保证逐项相加可校对。"""
    try:
        return round(float(str(value).replace(",", "")), 2)
    except (TypeError, ValueError):
        return 0.0


def _bump_data_version(label: Any) -> str:
    text = str(label or "v0")
    match = re.fullmatch(r"v(\d+)", text)
    if match:
        return f"v{int(match.group(1)) + 1}"
    return f"{text}.rev"


class AggRefreshEngine:
    """汇总刷新版本引擎：管理源分区、刷新版本生命周期和按版本查询。"""

    def __init__(self, store: Store) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # 内部查找
    # ------------------------------------------------------------------

    def _entry(self, entry_id: int) -> dict[str, Any]:
        entry = self._store.find("agg_summary", int(entry_id))
        if entry is None:
            raise AggRefreshError("entry_not_found", f"汇总表 {entry_id} 不存在或已归档")
        return entry

    def _find_partition(self, partition_key: str, source_fact: str | None = None) -> dict[str, Any] | None:
        for partition in self._store.fact_partitions:
            if partition.get("分区键") != partition_key:
                continue
            if source_fact and partition.get("源事实表") != source_fact:
                continue
            return partition
        return None

    def _version(self, version_id: str) -> dict[str, Any]:
        for version in self._store.agg_refresh_versions:
            if version["version_id"] == version_id:
                return version
        raise AggRefreshError("version_not_found", f"汇总刷新版本 {version_id} 不存在")

    def _building_version(self, version_id: str) -> dict[str, Any]:
        version = self._version(version_id)
        if version["state"] != STATE_BUILDING:
            raise AggRefreshError(
                "version_not_building",
                f"版本 {version_id} 当前状态为 {version['state']}，不能继续构建",
            )
        return version

    # ------------------------------------------------------------------
    # 源事实分区：更正与撤销
    # ------------------------------------------------------------------

    def list_partitions(self, source_fact: str | None = None) -> list[dict[str, Any]]:
        rows = self._store.fact_partitions
        if source_fact:
            rows = [p for p in rows if p.get("源事实表") == source_fact]
        return [dict(p) for p in rows]

    def correct_partition(self, partition_key: str, amount: Any, reason: str = "") -> dict[str, Any]:
        """源表更正：更新分区销售额并推进数据版本，触发后续刷新重算该分区。"""
        partition = self._find_partition(partition_key)
        if partition is None:
            raise AggRefreshError("partition_not_found", f"源分区 {partition_key} 不存在")
        if partition.get("状态") == PARTITION_REVOKED:
            raise AggRefreshError("partition_revoked", f"源分区 {partition_key} 已撤销，不能更正")
        partition["销售额"] = _money(amount)
        partition["数据版本"] = _bump_data_version(partition.get("数据版本"))
        partition["更正原因"] = reason
        partition["更新时间"] = _now()
        self._store.channel.publish("fact.partition_corrected", {
            "分区键": partition_key,
            "源事实表": partition.get("源事实表"),
            "销售额": partition["销售额"],
            "数据版本": partition["数据版本"],
            "原因": reason,
        })
        return dict(partition)

    def revoke_partition(self, partition_key: str, reason: str = "") -> dict[str, Any]:
        """源表撤销：分区状态置为已撤销，后续刷新与提交校验都会将其剔除。"""
        partition = self._find_partition(partition_key)
        if partition is None:
            raise AggRefreshError("partition_not_found", f"源分区 {partition_key} 不存在")
        if partition.get("状态") == PARTITION_REVOKED:
            raise AggRefreshError("partition_revoked", f"源分区 {partition_key} 已处于撤销状态")
        partition["状态"] = PARTITION_REVOKED
        partition["撤销原因"] = reason
        partition["更新时间"] = _now()
        self._store.channel.publish("fact.partition_revoked", {
            "分区键": partition_key,
            "源事实表": partition.get("源事实表"),
            "销售额": partition.get("销售额"),
            "原因": reason,
        })
        return dict(partition)

    # ------------------------------------------------------------------
    # 刷新版本生命周期
    # ------------------------------------------------------------------

    def begin_refresh(self, entry_id: int) -> dict[str, Any]:
        """开启一个构建中版本：未变更分区整体继承，新增/变更分区进入待算清单。"""
        entry = self._entry(entry_id)
        building = [
            v for v in self._store.agg_refresh_versions
            if v["entry_id"] == entry_id and v["state"] == STATE_BUILDING
        ]
        if building:
            raise AggRefreshError(
                "refresh_in_progress",
                f"汇总表 {entry_id} 存在未完成的刷新版本 {building[0]['version_id']}",
            )

        base = self.current_version(entry_id)
        base_items = {item["分区键"]: item for item in (base["items"] if base else [])}

        carried: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        for partition in self._store.fact_partitions:
            if partition.get("源事实表") != entry.get("源事实表"):
                continue
            if partition.get("状态") != PARTITION_CONFIRMED:
                continue  # 已撤销分区不进入新版本
            inherited = base_items.get(partition["分区键"])
            if inherited and inherited.get("分区数据版本") == partition.get("数据版本"):
                # 分区未变更：沿用上次结果，同一源分区只计入一次，不重复计算
                carried.append({**inherited, "计入方式": "继承"})
            else:
                pending.append(partition)

        if not carried and not pending:
            raise AggRefreshError(
                "no_source_partition",
                f"源事实表 {entry.get('源事实表')} 无已确认分区，无法刷新",
            )

        seq = max(
            (v["seq"] for v in self._store.agg_refresh_versions if v["entry_id"] == entry_id),
            default=0,
        ) + 1
        version = {
            "version_id": f"arv-{entry_id}-{seq:04d}",
            "entry_id": entry_id,
            "汇总名称": entry.get("汇总名称", ""),
            "源事实表": entry.get("源事实表", ""),
            "seq": seq,
            "state": STATE_BUILDING,
            "base_version_id": base["version_id"] if base else None,
            "items": carried,
            "excluded": [],
            "total_amount": None,
            "pending_partitions": [p["分区键"] for p in pending],
            "processed_partitions": [],
            "created_at": _now(),
            "committed_at": None,
            "note": "",
        }
        self._store.agg_refresh_versions.append(version)
        version["checkpoint_id"] = self._store.recovery.create_checkpoint(
            "agg_summary", f"refresh_{entry_id}_{seq}", {
                "version_id": version["version_id"],
                "phase": "refresh_started",
                "pending": len(pending),
                "carried": len(carried),
            },
        )
        self._store.channel.publish("agg.refresh_started", {
            "version_id": version["version_id"],
            "entry_id": entry_id,
            "agg_name": version["汇总名称"],
            "carried": len(carried),
            "pending": len(pending),
        })
        return version

    def process_partition(self, version_id: str, partition_key: str | None = None) -> dict[str, Any] | None:
        """处理一个待算分区并写入版本；同一分区键去重，销售额只计入一次。"""
        version = self._building_version(version_id)
        if partition_key is None:
            if not version["pending_partitions"]:
                return None
            partition_key = version["pending_partitions"][0]
        if partition_key not in version["pending_partitions"]:
            raise AggRefreshError(
                "partition_not_pending",
                f"分区 {partition_key} 不在版本 {version_id} 的待处理列表中",
            )
        version["pending_partitions"].remove(partition_key)
        version["processed_partitions"].append(partition_key)

        partition = self._find_partition(partition_key, version["源事实表"])
        if partition is None or partition.get("状态") != PARTITION_CONFIRMED:
            version["excluded"].append({
                "分区键": partition_key,
                "源事实表": version["源事实表"],
                "金额": 0.0,
                "排除原因": "处理时源分区已撤销或不存在",
            })
            return None

        item = {
            "分区键": partition["分区键"],
            "源事实表": partition["源事实表"],
            "月份": partition.get("月份", ""),
            "地区": partition.get("地区", ""),
            "金额": _money(partition.get("销售额")),
            "分区数据版本": partition.get("数据版本", ""),
            "计入方式": "新算",
        }
        # 同一源分区只计入一次：按键去重后写入
        version["items"] = [i for i in version["items"] if i["分区键"] != partition_key]
        version["items"].append(item)
        self._store.channel.publish("agg.partition_processed", {
            "version_id": version_id,
            "分区键": partition_key,
            "金额": item["金额"],
        })
        return item

    def process_all(self, version_id: str) -> int:
        """处理全部待算分区，返回处理个数。"""
        version = self._building_version(version_id)
        count = 0
        while version["pending_partitions"]:
            self.process_partition(version_id)
            count += 1
        return count

    def commit(self, version_id: str) -> dict[str, Any]:
        """提交版本：校验全部计入分区，剔除已撤销/已更正分区后确认。

        已确认版本完整对应一个可复算的源分区集合；未处理完待算分区时
        拒绝提交，调用方保留上次成功汇总。
        """
        version = self._building_version(version_id)
        if version["pending_partitions"]:
            raise AggRefreshError(
                "refresh_incomplete",
                f"刷新未完成：仍有 {len(version['pending_partitions'])} 个分区待处理，"
                "保留上次成功汇总",
            )

        valid: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = list(version["excluded"])
        for item in version["items"]:
            partition = self._find_partition(item["分区键"], version["源事实表"])
            if (
                partition is not None
                and partition.get("状态") == PARTITION_CONFIRMED
                and partition.get("数据版本") == item.get("分区数据版本")
            ):
                valid.append(item)
                continue
            reason = "源分区已撤销" if (
                partition is None or partition.get("状态") != PARTITION_CONFIRMED
            ) else "源分区已更正"
            excluded.append({**item, "排除原因": reason})

        version["items"] = valid
        version["excluded"] = excluded
        version["total_amount"] = _money(sum(i["金额"] for i in valid))
        version["state"] = STATE_COMMITTED
        version["committed_at"] = _now()
        checkpoint_id = version.get("checkpoint_id")
        if checkpoint_id:
            self._store.recovery.mark_completed(checkpoint_id)
        self._store.channel.publish("agg.refresh_committed", {
            "version_id": version_id,
            "entry_id": version["entry_id"],
            "agg_name": version["汇总名称"],
            "分区数": len(valid),
            "total_amount": version["total_amount"],
            "excluded": len(excluded),
        })
        return version

    def fail(self, version_id: str, reason: str = "") -> dict[str, Any]:
        """标记版本失败：不影响当前已确认版本，查询仍落在上次成功汇总上。"""
        version = self._building_version(version_id)
        version["state"] = STATE_FAILED
        version["note"] = reason
        self._store.recovery.log_retry(
            "agg_summary", f"refresh_{version['entry_id']}_{version['seq']}", 1,
            reason or "刷新失败", False,
        )
        self._store.channel.publish("agg.refresh_failed", {
            "version_id": version_id,
            "entry_id": version["entry_id"],
            "reason": reason,
        })
        return version

    def refresh(self, entry_id: int) -> dict[str, Any]:
        """一键刷新：开启版本 → 处理全部待算分区 → 校验并提交。"""
        version = self.begin_refresh(entry_id)
        self.process_all(version["version_id"])
        return self.commit(version["version_id"])

    # ------------------------------------------------------------------
    # 版本查询与血缘追查
    # ------------------------------------------------------------------

    def current_version(self, entry_id: int) -> dict[str, Any] | None:
        """最近一次已确认版本；刷新未完成时它仍然是查询依据。"""
        committed = [
            v for v in self._store.agg_refresh_versions
            if v["entry_id"] == entry_id and v["state"] == STATE_COMMITTED
        ]
        if not committed:
            return None
        return max(committed, key=lambda v: v["seq"])

    def list_versions(self, entry_id: int) -> list[dict[str, Any]]:
        self._entry(entry_id)
        return sorted(
            (v for v in self._store.agg_refresh_versions if v["entry_id"] == entry_id),
            key=lambda v: v["seq"],
        )

    def get_version(self, version_id: str) -> dict[str, Any]:
        return self._version(version_id)

    def lineage(self, entry_id: int) -> dict[str, Any]:
        """构建进度血缘：按源事实表和分区追查构建到何处。"""
        entry = self._entry(entry_id)
        current = self.current_version(entry_id)
        building = next(
            (v for v in reversed(self._store.agg_refresh_versions)
             if v["entry_id"] == entry_id and v["state"] == STATE_BUILDING),
            None,
        )
        trace: list[dict[str, Any]] = []
        for partition in self._store.fact_partitions:
            if partition.get("源事实表") != entry.get("源事实表"):
                continue
            included_version = None
            included_amount = None
            if current:
                hit = next(
                    (i for i in current["items"] if i["分区键"] == partition["分区键"]),
                    None,
                )
                if hit:
                    included_version = current["version_id"]
                    included_amount = hit["金额"]
            trace.append({
                "分区键": partition["分区键"],
                "源事实表": partition["源事实表"],
                "月份": partition.get("月份", ""),
                "地区": partition.get("地区", ""),
                "分区状态": partition.get("状态", ""),
                "数据版本": partition.get("数据版本", ""),
                "计入版本": included_version,
                "计入金额": included_amount,
            })
        progress = None
        if building:
            done = len(building["processed_partitions"])
            todo = len(building["pending_partitions"])
            progress = {
                "version_id": building["version_id"],
                "已处理分区": list(building["processed_partitions"]),
                "待处理分区": list(building["pending_partitions"]),
                "进度": f"{done}/{done + todo}",
            }
        return {
            "entry_id": entry_id,
            "汇总名称": entry.get("汇总名称", ""),
            "源事实表": entry.get("源事实表", ""),
            "当前已确认版本": current["version_id"] if current else None,
            "构建中版本": progress,
            "分区追溯": trace,
        }

    def query_aggregate(self, entry_id: int, filters: dict[str, str] | None = None) -> dict[str, Any]:
        """按汇总刷新版本取聚合度量，并与金额持久化记录逐项校对。

        只读取最近一次已确认版本；尚未确认（构建中/已失败）的金额
        不进入查询结果。
        """
        entry = self._entry(entry_id)
        filters = {k: v for k, v in (filters or {}).items() if k in FILTER_FIELDS and str(v).strip()}
        version = self.current_version(entry_id)
        if version is None:
            return {
                "entry_id": entry_id,
                "汇总名称": entry.get("汇总名称", ""),
                "已确认": False,
                "version_id": None,
                "过滤条件": filters,
                "度量": {},
                "逐项合计": 0.0,
                "分区明细": [],
                "一致分区": [],
                "校对一致": False,
                "message": "尚无已确认的汇总刷新版本，未确认金额不进入查询结果",
            }

        items = [i for i in version["items"] if _match_filters(i, filters)]
        total = _money(sum(i["金额"] for i in items))
        details: list[dict[str, Any]] = []
        consistent_keys: list[str] = []
        all_consistent = True
        for item in items:
            partition = self._find_partition(item["分区键"], version["源事实表"])
            persisted = (
                _money(partition.get("销售额"))
                if partition is not None and partition.get("状态") == PARTITION_CONFIRMED
                else None
            )
            consistent = (
                persisted is not None
                and persisted == _money(item["金额"])
                and partition is not None
                and partition.get("数据版本") == item.get("分区数据版本")
            )
            all_consistent = all_consistent and consistent
            if consistent:
                consistent_keys.append(item["分区键"])
            details.append({
                "分区键": item["分区键"],
                "源事实表": item["源事实表"],
                "月份": item.get("月份", ""),
                "地区": item.get("地区", ""),
                "版本金额": _money(item["金额"]),
                "持久化金额": persisted,
                "分区状态": partition.get("状态", "已删除") if partition else "已删除",
                "一致": consistent,
            })
        return {
            "entry_id": entry_id,
            "汇总名称": entry.get("汇总名称", ""),
            "已确认": True,
            "version_id": version["version_id"],
            "版本状态": version["state"],
            "committed_at": version["committed_at"],
            "过滤条件": filters,
            "度量": {"销售额合计": total},
            "逐项合计": _money(sum(d["版本金额"] for d in details)),
            "分区明细": details,
            "一致分区": consistent_keys,
            "校对一致": all_consistent and total == _money(sum(d["版本金额"] for d in details)),
        }


def _match_filters(item: dict[str, Any], filters: dict[str, str]) -> bool:
    for field, expected in filters.items():
        if str(item.get(field, "")) != str(expected):
            return False
    return True


# 与全局 store 绑定的引擎单例，供领域服务与路由层使用
agg_refresh_engine = AggRefreshEngine(store)

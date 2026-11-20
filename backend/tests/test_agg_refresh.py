"""汇总刷新版本引擎测试：版本化刷新、幂等计入、原子提交、
构建进度追查、中途撤销源分区、星型查询按版本取数与逐项校对。

场景对应：
- 参与聚合的源分区和产出金额写入汇总刷新版本，供星型查询复算
- 同一源分区的销售额只能计入一次
- 刷新未完成时保留上次成功汇总，构建进度可按源事实表和分区追查
- 尚未确认的金额不能进入查询结果
- 汇总跑到一半源表更正并撤销上月华东分区，最终结果仍完整对应
  一个可复算的源分区集合，查询金额与版本分区逐项相加校对
"""
from __future__ import annotations

import pytest

from app.store import Store
from app.services.agg_refresh import (
    AggRefreshEngine,
    AggRefreshError,
    STATE_BUILDING,
    STATE_COMMITTED,
    STATE_FAILED,
)

ENTRY_ID = 7  # 种子汇总：华东月销售汇总（源事实表=销售明细事实，汇总维度=月份.地区）
HD_10 = "dt=2026-10/region=华东"  # 上月华东分区
HB_10 = "dt=2026-10/region=华北"
HD_11 = "dt=2026-11/region=华东"
HB_11 = "dt=2026-11/region=华北"

TOTAL_ALL = 1285000.00 + 864000.00 + 733500.00 + 412000.00 + 296000.00 + 188000.00
HD_ALL = 1285000.00 + 412000.00


@pytest.fixture
def engine() -> AggRefreshEngine:
    """每个测试使用独立 store 与引擎实例。"""
    return AggRefreshEngine(Store())


# ======================================================================
# Section A: 刷新版本写入源分区与金额
# ======================================================================


class TestRefreshVersion:
    """参与聚合的源分区和产出的金额写入汇总刷新版本。"""

    def test_refresh_commits_version_with_partitions_and_amounts(self, engine: AggRefreshEngine) -> None:
        version = engine.refresh(ENTRY_ID)
        assert version["state"] == STATE_COMMITTED
        assert version["committed_at"]

        keys = {item["分区键"] for item in version["items"]}
        expected = {p["分区键"] for p in engine.list_partitions("销售明细事实") if p["状态"] == "已确认"}
        assert keys == expected
        for item in version["items"]:
            assert item["源事实表"] == "销售明细事实"
            assert item["金额"] > 0
            assert item["分区数据版本"] == "v1"
        # 版本总额 = 分区金额逐项相加
        assert version["total_amount"] == pytest.approx(TOTAL_ALL)
        assert version["total_amount"] == pytest.approx(sum(i["金额"] for i in version["items"]))

    def test_incremental_refresh_inherits_unchanged_partitions(self, engine: AggRefreshEngine) -> None:
        """增量刷新：未变更分区整体继承，只有更正过的分区重算。"""
        v1 = engine.refresh(ENTRY_ID)
        engine.correct_partition(HD_10, 1300000.00, "源表更正")
        v2 = engine.refresh(ENTRY_ID)

        assert v2["state"] == STATE_COMMITTED
        assert v2["base_version_id"] == v1["version_id"]
        by_key = {i["分区键"]: i for i in v2["items"]}
        assert by_key[HD_10]["计入方式"] == "新算"
        assert by_key[HD_10]["金额"] == pytest.approx(1300000.00)
        assert by_key[HD_10]["分区数据版本"] == "v2"
        assert by_key[HB_10]["计入方式"] == "继承"
        assert v2["total_amount"] == pytest.approx(TOTAL_ALL - 1285000.00 + 1300000.00)

    def test_same_partition_counted_only_once(self, engine: AggRefreshEngine) -> None:
        """同一源分区的销售额只能计入一次：重复处理被拒绝，版本内按键去重。"""
        engine.refresh(ENTRY_ID)
        engine.correct_partition(HD_10, 1300000.00)
        v2 = engine.begin_refresh(ENTRY_ID)

        item = engine.process_partition(v2["version_id"], HD_10)
        assert item is not None and item["金额"] == pytest.approx(1300000.00)
        # 同一分区不允许重复处理
        with pytest.raises(AggRefreshError):
            engine.process_partition(v2["version_id"], HD_10)

        engine.process_all(v2["version_id"])
        committed = engine.commit(v2["version_id"])
        occurrences = [i for i in committed["items"] if i["分区键"] == HD_10]
        assert len(occurrences) == 1
        assert committed["total_amount"] == pytest.approx(sum(i["金额"] for i in committed["items"]))

    def test_concurrent_refresh_rejected(self, engine: AggRefreshEngine) -> None:
        engine.begin_refresh(ENTRY_ID)
        with pytest.raises(AggRefreshError):
            engine.begin_refresh(ENTRY_ID)


# ======================================================================
# Section B: 原子提交与进度追查
# ======================================================================


class TestAtomicCommit:
    """刷新未完成时保留上次成功汇总；构建到何处可按源事实表和分区追查。"""

    def test_incomplete_refresh_keeps_last_committed(self, engine: AggRefreshEngine) -> None:
        v1 = engine.refresh(ENTRY_ID)
        engine.correct_partition(HD_10, 1300000.00)
        v2 = engine.begin_refresh(ENTRY_ID)

        # 待算分区未处理完，拒绝提交
        with pytest.raises(AggRefreshError):
            engine.commit(v2["version_id"])
        engine.fail(v2["version_id"], "刷新中止")
        assert v2["state"] == STATE_FAILED

        # 当前版本仍是上次成功汇总
        current = engine.current_version(ENTRY_ID)
        assert current is not None and current["version_id"] == v1["version_id"]
        result = engine.query_aggregate(ENTRY_ID, {"地区": "华东", "月份": "2026-10"})
        assert result["version_id"] == v1["version_id"]
        assert result["度量"]["销售额合计"] == pytest.approx(1285000.00)

    def test_uncommitted_amounts_stay_out_of_query(self, engine: AggRefreshEngine) -> None:
        """尚未确认的金额不能进入查询结果。"""
        # 无已确认版本：查不到任何金额
        result = engine.query_aggregate(ENTRY_ID, {"地区": "华东"})
        assert result["已确认"] is False
        assert result["分区明细"] == []
        assert result["度量"] == {}

        v1 = engine.refresh(ENTRY_ID)
        engine.correct_partition(HD_10, 990000.00)
        v2 = engine.begin_refresh(ENTRY_ID)
        engine.process_all(v2["version_id"])  # 构建中，未提交
        assert v2["state"] == STATE_BUILDING

        # 查询仍落在已确认版本上，看不到构建中的新金额
        result = engine.query_aggregate(ENTRY_ID, {"地区": "华东", "月份": "2026-10"})
        assert result["version_id"] == v1["version_id"]
        assert result["度量"]["销售额合计"] == pytest.approx(1285000.00)

    def test_lineage_tracks_progress_by_fact_and_partition(self, engine: AggRefreshEngine) -> None:
        engine.refresh(ENTRY_ID)
        engine.correct_partition(HD_10, 1300000.00)
        engine.correct_partition(HB_10, 870000.00)
        v2 = engine.begin_refresh(ENTRY_ID)
        engine.process_partition(v2["version_id"], HD_10)

        lineage = engine.lineage(ENTRY_ID)
        assert lineage["源事实表"] == "销售明细事实"
        building = lineage["构建中版本"]
        assert building is not None
        assert building["已处理分区"] == [HD_10]
        assert building["待处理分区"] == [HB_10]
        assert building["进度"] == "1/2"

        trace = {t["分区键"]: t for t in lineage["分区追溯"]}
        assert set(trace) == {p["分区键"] for p in engine.list_partitions("销售明细事实")}
        assert trace[HD_10]["计入版本"] == lineage["当前已确认版本"]
        assert trace[HD_10]["计入金额"] == pytest.approx(1285000.00)


# ======================================================================
# Section C: 中途撤销源分区（核心场景）
# ======================================================================


class TestMidRefreshRevoke:
    """汇总跑到一半，源表更正并撤销上月华东销售分区。"""

    def test_revoke_mid_refresh_excludes_partition_and_stays_recomputable(
        self, engine: AggRefreshEngine
    ) -> None:
        v1 = engine.refresh(ENTRY_ID)
        assert any(i["分区键"] == HD_10 for i in v1["items"])

        # 源表更正：推进数据版本，使该分区进入待算清单
        engine.correct_partition(HD_10, 1300000.00, "源表更正")
        v2 = engine.begin_refresh(ENTRY_ID)

        # 刷新跑到一半：该分区的部分金额已经算出
        partial = engine.process_partition(v2["version_id"], HD_10)
        assert partial is not None and partial["金额"] == pytest.approx(1300000.00)

        # 源表撤销上月华东分区，随后完成剩余刷新并提交
        engine.revoke_partition(HD_10, "源表更正撤销上月华东销售分区")
        engine.process_all(v2["version_id"])
        committed = engine.commit(v2["version_id"])

        assert committed["state"] == STATE_COMMITTED
        keys = {i["分区键"] for i in committed["items"]}
        assert HD_10 not in keys
        excluded = {i["分区键"] for i in committed["excluded"]}
        assert HD_10 in excluded

        # 最终结果完整对应一个可复算的源分区集合
        partitions = {p["分区键"]: p for p in engine.list_partitions("销售明细事实")}
        for item in committed["items"]:
            partition = partitions[item["分区键"]]
            assert partition["状态"] == "已确认"
            assert partition["数据版本"] == item["分区数据版本"]
            assert partition["销售额"] == pytest.approx(item["金额"])
        assert committed["total_amount"] == pytest.approx(sum(i["金额"] for i in committed["items"]))
        assert committed["total_amount"] == pytest.approx(TOTAL_ALL - 1285000.00)

        # 刷新后再查华东月销售汇总：金额与版本列出的分区逐项相加校对
        result = engine.query_aggregate(ENTRY_ID, {"地区": "华东"})
        assert result["version_id"] == committed["version_id"]
        assert result["校对一致"] is True
        assert {d["分区键"] for d in result["分区明细"]} == {HD_11}
        assert result["度量"]["销售额合计"] == pytest.approx(412000.00)
        assert result["逐项合计"] == pytest.approx(sum(d["版本金额"] for d in result["分区明细"]))

        # 被撤销的上月华东分区不再进入查询结果
        oct_result = engine.query_aggregate(ENTRY_ID, {"地区": "华东", "月份": "2026-10"})
        assert oct_result["分区明细"] == []
        assert oct_result["度量"]["销售额合计"] == 0.0

    def test_revoke_before_refresh_drops_partition(self, engine: AggRefreshEngine) -> None:
        engine.refresh(ENTRY_ID)
        engine.revoke_partition(HB_11, "源表撤销")
        v2 = engine.refresh(ENTRY_ID)
        assert HB_11 not in {i["分区键"] for i in v2["items"]}
        result = engine.query_aggregate(ENTRY_ID, {"地区": "华北"})
        assert {d["分区键"] for d in result["分区明细"]} == {HB_10}
        assert result["度量"]["销售额合计"] == pytest.approx(864000.00)

    def test_revoke_unknown_partition_rejected(self, engine: AggRefreshEngine) -> None:
        with pytest.raises(AggRefreshError):
            engine.revoke_partition("dt=2099-01/region=不存在")


# ======================================================================
# Section D: 星型查询按版本取数与逐项校对
# ======================================================================


class TestStarQueryReconcile:
    """星型查询按汇总刷新版本取聚合度量，返回与金额持久化记录一致的事实分区。"""

    def test_query_reconciles_item_by_item(self, engine: AggRefreshEngine) -> None:
        engine.refresh(ENTRY_ID)
        result = engine.query_aggregate(ENTRY_ID, {"地区": "华东"})
        assert result["已确认"] is True
        assert result["校对一致"] is True
        assert result["度量"]["销售额合计"] == pytest.approx(HD_ALL)
        assert result["逐项合计"] == pytest.approx(result["度量"]["销售额合计"])
        assert set(result["一致分区"]) == {HD_10, HD_11}
        for detail in result["分区明细"]:
            assert detail["一致"] is True
            assert detail["持久化金额"] == pytest.approx(detail["版本金额"])
            assert detail["分区状态"] == "已确认"

    def test_query_without_version_returns_empty(self, engine: AggRefreshEngine) -> None:
        result = engine.query_aggregate(ENTRY_ID)
        assert result["已确认"] is False
        assert "未确认" in result["message"]


# ======================================================================
# Section E: 服务层集成（全局 store：动作 + 星型查询执行）
# ======================================================================


class TestServiceIntegration:
    """agg_summary 动作驱动引擎；star_query 按已确认版本执行。"""

    def test_refresh_action_commits_version_and_star_query_reads_it(self) -> None:
        from app.services import service_for
        from app.services.agg_refresh import agg_refresh_engine

        agg_service = service_for("agg_summary")
        entry, message = agg_service.run_action(ENTRY_ID, "刷新汇总")
        assert entry is not None and entry["status"] == "已刷新"
        assert entry["abnormal"] is False

        version = agg_refresh_engine.current_version(ENTRY_ID)
        assert version is not None and version["state"] == STATE_COMMITTED
        assert version["total_amount"] == pytest.approx(TOTAL_ALL)

        star_service = service_for("star_query")
        result, exec_message = star_service.execute_query(6, {"地区": "华东"})
        assert result is not None
        assert result["命中汇总"] == "华东月销售汇总"
        assert result["version_id"] == version["version_id"]
        assert result["度量"]["销售额合计"] == pytest.approx(HD_ALL)
        assert result["校对一致"] is True

    def test_failed_refresh_keeps_last_successful_summary(self) -> None:
        from app.services import service_for

        agg_service = service_for("agg_summary")
        # 每周仓储快照的源事实表没有任何已确认分区 → 刷新无法完成
        entry, message = agg_service.run_action(2, "刷新汇总")
        assert entry is not None
        assert entry["status"] == "已构建"  # 状态不推进，保留上次成功汇总
        assert entry["abnormal"] is True
        assert "保留上次成功汇总" in message

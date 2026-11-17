"""渲染超时回收 / 结果新鲜度 / 容量释放 / 调度侧消费的回归测试。

用标准库 unittest 编写，直接调用服务层和 FastAPI 路由；
每个用例前重置内存仓库，避免示例数据互相污染。
"""
from __future__ import annotations

import sys
import threading
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.main import app as fastapi_app
from app.services import renderledger
from app.services.dispatch import DispatchService
from app.services.rendertask import RendertaskService
from app.services.worker import WorkerService
from app.store import store
from app.timeutil import iso, now


def reset_store() -> None:
    """恢复示例数据：测试里会大量改写账本，每个用例都从干净状态开始。"""
    store.__init__()


def job(job_id: int) -> dict:
    found = store.find("rendertask", job_id)
    assert found is not None
    return found


class RenderLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        self.rt = RendertaskService()
        self.dispatch = DispatchService()
        self.worker = WorkerService()

    def _enqueue_and_start(self, job_id: int = 1) -> dict:
        entry = job(job_id)
        if entry.get("status") not in {"排队中", "渲染中"}:
            _, message = self.rt.run_action(job_id, "提交排队")
            self.assertIn("已排队", message)
        entry, message = self.rt.run_action(job_id, "启动渲染")
        self.assertIsNotNone(entry, message)
        self.assertIn("承接节点", message)
        return entry

    def test_reap_settles_expired_attempt_and_releases_capacity_once(self) -> None:
        entry = job(3)
        worker_before = next(row for row in store.rows("worker") if row.get("节点编号") == "WORK-0003")
        self.assertEqual(worker_before["已占用槽位"], 1)

        report = renderledger.reap_timeouts()
        self.assertGreaterEqual(report["已回收"], 1)
        reaped = next(item for item in report["items"] if item["作业编号"] == "REND-0003")
        self.assertEqual(reaped["回收类型"], "渲染超时")
        # 等待策略 domcontentloaded 给 5 秒宽限
        self.assertEqual(reaped["宽限秒"], 5)
        self.assertEqual(reaped["生效阈值秒"], 125)
        self.assertTrue(reaped["是否重排"])
        self.assertFalse(reaped["结果生效"])
        self.assertEqual(job(3)["当前尝试版本"], 2)

        releases = [row for row in store.rows("capacity_release") if row.get("作业编号") == "REND-0003"]
        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0]["尝试版本"], 1)

        # 第二次回收不能重复结算同一个尝试，也不能再登记释放
        second = renderledger.reap_timeouts()
        self.assertEqual(second["已回收"], 0)
        self.assertEqual(
            len([row for row in store.rows("capacity_release") if row.get("作业编号") == "REND-0003"]),
            1,
        )

    def test_completed_job_never_rewritten_by_late_timeout(self) -> None:
        self._enqueue_and_start(1)
        entry, message = self.rt.run_action(1, "提交渲染结果", {"结果内容": {"页面标题": "完成"}})
        self.assertEqual(entry["status"], "已完成", message)
        version = job(1)["生效结果版本"]

        report = renderledger.reap_timeouts(job_id=1)
        self.assertEqual(report["已回收"], 0)
        self.assertEqual(job(1)["status"], "已完成")
        self.assertEqual(job(1)["生效结果版本"], version)
        self.assertEqual(renderledger.results_of(1)[0]["结果状态"], "已完成")

    def test_late_callback_only_archives_stale_result(self) -> None:
        self._enqueue_and_start(1)
        # 模拟旧一轮的尝试版本回写：不应覆盖当前渲染中的作业
        entry, message = self.rt.run_action(1, "提交渲染结果", {"尝试版本": 99})
        self.assertIsNone(entry)
        self.assertIn("不存在尝试", message)

        self.rt.run_action(1, "终止渲染")
        self.rt.run_action(1, "重新排队")
        self._enqueue_and_start(1)
        old_version = job(1)["当前尝试版本"] - 1
        entry, message = self.rt.run_action(1, "提交渲染结果", {"尝试版本": old_version})
        self.assertIsNotNone(entry, message)
        self.assertEqual(job(1)["status"], "渲染中")
        stale = [row for row in renderledger.results_of(1) if row["尝试版本"] == old_version]
        self.assertTrue(stale)
        self.assertFalse(any(row["生效"] for row in stale))

    def test_old_page_callback_and_new_round_finish_together_have_single_effective(self) -> None:
        """旧页面回调与新一轮渲染同时完成：两份结果指向不同尝试，只有新的生效。"""
        self.rt.run_action(1, "提交排队")
        self.rt.run_action(1, "启动渲染")
        old_version = job(1)["当前尝试版本"]
        self.rt.run_action(1, "终止渲染")
        self.rt.run_action(1, "重新排队")
        self.rt.run_action(1, "启动渲染")
        new_version = job(1)["当前尝试版本"]
        self.assertNotEqual(old_version, new_version)

        barrier = threading.Barrier(2)
        outcomes: list[tuple] = []

        def callback(version: int, title: str) -> None:
            barrier.wait()
            outcomes.append(
                self.rt.run_action(1, "提交渲染结果", {"尝试版本": version, "结果内容": {"页面标题": title}})
            )

        threads = [
            threading.Thread(target=callback, args=(old_version, "旧页面")),
            threading.Thread(target=callback, args=(new_version, "新页面")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(outcomes), 2)
        results = renderledger.results_of(1)
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(1 for row in results if row["生效"]), 1)
        effective = next(row for row in results if row["生效"])
        self.assertEqual(effective["尝试版本"], new_version)
        self.assertEqual(job(1)["状态"] if "状态" in job(1) else job(1)["status"], "已完成")

    def test_timeout_completion_and_offline_race_leave_single_result(self) -> None:
        self._enqueue_and_start(1)
        expired = iso(now() + timedelta(seconds=10_000))
        barrier = threading.Barrier(3)
        errors: list[BaseException] = []

        def guard(fn) -> None:
            try:
                barrier.wait()
                fn()
            except BaseException as error:  # noqa: BLE001 - 测试里收集异常再统一断言
                errors.append(error)

        threads = [
            threading.Thread(
                target=guard,
                args=(lambda: renderledger.reap_timeouts(job_id=1, at_iso=expired),),
            ),
            threading.Thread(
                target=guard,
                args=(
                    lambda: self.rt.run_action(
                        1, "提交渲染结果", {"尝试版本": 1, "结果内容": {"页面标题": "完成"}}
                    ),
                ),
            ),
            threading.Thread(
                target=guard,
                args=(lambda: self.rt.run_action(3, "标记离线"),),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])

        # REND-0001 与 WORK-0003 上的 REND-0003 都会参与竞争；逐个作业断言唯一结果。
        results = renderledger.results_of(1)
        self.assertEqual(len(results), 1)
        releases = [row for row in store.rows("capacity_release") if row.get("作业id") == 1]
        self.assertEqual(len(releases), 1)
        # 超时回收先把 REND-0001 判超时并重排；随后的完成回调只能归档，不能改写新尝试。
        self.assertIn(job(1)["status"], {"排队中", "已完成", "已超时"})
        effective = [row for row in results if row["生效"]]
        self.assertLessEqual(len(effective), 1)
        # 节点离线同时也会结算 WORK-0003 上的 REND-0003；每个尝试各自最多一条释放。
        for attempt in store.rows("render_attempt"):
            if attempt.get("容量已释放"):
                count = len(
                    [
                        row
                        for row in store.rows("capacity_release")
                        if row.get("尝试id") == attempt.get("id")
                    ]
                )
                self.assertLessEqual(count, 1)

    def test_repeated_terminate_does_not_release_capacity_twice(self) -> None:
        self._enqueue_and_start(1)
        self.rt.run_action(1, "终止渲染")
        first = len([row for row in store.rows("capacity_release") if row.get("作业id") == 1])
        self.assertEqual(first, 1)
        entry, message = self.rt.run_action(1, "终止渲染")
        self.assertIn("重复终止", message)
        self.assertIsNotNone(entry)
        second = len([row for row in store.rows("capacity_release") if row.get("作业id") == 1])
        self.assertEqual(second, 1)

    def test_duplicate_completion_is_idempotent(self) -> None:
        self._enqueue_and_start(1)
        first, _ = self.rt.run_action(
            1, "提交渲染结果", {"尝试版本": 1, "幂等键": "job-1-attempt", "结果内容": {"内容长度": 12}}
        )
        second, message = self.rt.run_action(
            1, "提交渲染结果", {"尝试版本": 1, "幂等键": "job-1-attempt", "结果内容": {"内容长度": 12}}
        )
        self.assertEqual(first["生效结果版本"], second["生效结果版本"])
        self.assertIn("幂等", message)
        self.assertEqual(len(renderledger.results_of(1)), 1)

    def test_concurrent_completion_saves_single_result(self) -> None:
        self._enqueue_and_start(1)
        barrier = threading.Barrier(4)
        messages: list[str] = []
        lock = threading.Lock()

        def complete() -> None:
            barrier.wait()
            _, message = self.rt.run_action(
                1, "提交渲染结果", {"尝试版本": 1, "结果内容": {"页面标题": "并发"}}
            )
            with lock:
                messages.append(message)

        threads = [threading.Thread(target=complete) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(messages), 4)
        self.assertEqual(len(renderledger.results_of(1)), 1)
        self.assertEqual(job(1)["status"], "已完成")

    def test_result_matches_input_field_by_field(self) -> None:
        self._enqueue_and_start(1)
        self.rt.run_action(1, "提交渲染结果", {"结果内容": {"页面标题": "核对"}})
        results = self.rt.results(1)
        self.assertEqual(len(results), 1)
        check = results[0]["输入核对"]
        self.assertTrue(check["ok"], check["mismatched"])
        fields = {item["字段"] for item in check["items"]}
        self.assertEqual(fields, set(renderledger.RESULT_ECHO_FIELDS))
        for item in check["items"]:
            self.assertEqual(str(item["输入"]), str(item["结果"]))

    def test_requeue_count_and_dispatch_explanation(self) -> None:
        self.rt.run_action(1, "提交排队")
        self.rt.run_action(1, "启动渲染")
        self.rt.run_action(1, "终止渲染")
        self.rt.run_action(1, "重新排队")
        self.assertEqual(job(1)["重排次数"], 1)

        plan = self.dispatch.plan()
        candidate = next(item for item in plan["candidates"] if item["作业编号"] == "REND-0001")
        self.assertEqual(candidate["重排次数"], 1)
        # 尝试次数 2 次再扣 20s：等待时长、重排次数、历史尝试共同决定得分
        self.assertEqual(candidate["派发得分"], candidate["已等待秒"] - 60 - 20)
        self.assertIn("重排 1 次扣 60s", candidate["排队公平性"])
        self.assertIn("历史尝试 2 次扣 20s", candidate["排队公平性"])
        self.assertIsNotNone(plan["建议节点"] if plan["可派发"] else plan["decision"])

    def test_dispatch_consumes_release_and_manifest_records_version(self) -> None:
        entry = job(3)
        self.rt.run_action(3, "终止渲染")
        self.assertEqual(job(3)["生效结果版本"], 1)

        sync = self.dispatch.sync()
        self.assertGreaterEqual(sync["manifest"]["清单版本"], 1)
        self.assertEqual(sync["manifest"]["已消费结果版本"], 1)
        self.assertGreaterEqual(sync["record"]["消费结果条数"], 1)
        # 示例数据里 REND-0002 的首次尝试本来就有一条待消费的释放
        self.assertGreaterEqual(sync["record"]["消费释放条数"], 1)
        self.assertIn(1, sync["record"]["生效结果版本"])
        result = next(row for row in store.rows("render_result") if row["作业编号"] == "REND-0003")
        self.assertTrue(result["调度消费"])
        self.assertEqual(result["消费清单版本"], sync["manifest"]["清单版本"])

        # 节点容量：占用从 1 归还到 0
        node = next(row for row in sync["nodes"] if row["节点标识"] == "WORK-0003")
        self.assertEqual(node["已占用槽位"], 0)
        self.assertEqual(node["剩余槽位"], 3)

        again = self.dispatch.sync()
        self.assertIsNone(again["record"])
        self.assertEqual(again["manifest"]["清单版本"], sync["manifest"]["清单版本"])

    def test_release_consume_is_idempotent(self) -> None:
        self.rt.run_action(3, "终止渲染")
        first = self.worker.consume_capacity_releases()
        self.assertGreaterEqual(first["消费条数"], 1)
        second = self.worker.consume_capacity_releases()
        self.assertEqual(second["消费条数"], 0)
        self.assertTrue(second["skipped"])
        self.assertEqual(
            next(row for row in store.rows("worker") if row.get("节点编号") == "WORK-0003")["已占用槽位"],
            0,
        )

    def test_node_offline_settles_attempts_and_sends_back_to_queue(self) -> None:
        self.rt.run_action(3, "提交渲染结果", {"结果内容": {"页面标题": "先完成"}})
        self._enqueue_and_start(2)
        entry, message = self.worker.run_action(3, "标记离线")
        self.assertIsNotNone(entry, message)
        self.assertIn("结算", message)
        # REND-0002 在 WORK-0003 上渲染中，节点离线后按重排上限回到排队
        self.assertEqual(job(2)["status"], "排队中")
        served = job(2)["当前尝试版本"] - 1
        releases = [
            row
            for row in store.rows("capacity_release")
            if row.get("作业id") == 2 and row.get("尝试版本") == served
        ]
        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0]["释放原因"], "节点 WORK-0003 离线")

    def test_state_flow_is_one_way(self) -> None:
        self.rt.run_action(1, "提交排队")
        entry, message = self.rt.run_action(1, "启动渲染")
        self.assertEqual(job(1)["status"], "渲染中")
        # 已完成之后不能再启动渲染
        self.rt.run_action(1, "提交渲染结果", {})
        blocked, message = self.rt.run_action(1, "启动渲染")
        self.assertIsNone(blocked)
        self.assertIn("不能启动渲染", message)
        # 归档后不能再排队
        archived, message = self.rt.archive(job(1))
        self.assertIsNotNone(archived, message)
        blocked, message = self.rt.run_action(1, "提交排队")
        self.assertIsNone(blocked)
        self.assertIn("已归档", message)


class RenderLedgerApiTest(unittest.TestCase):
    """按真实 HTTP 路径验证四个新接口的接线。"""

    def setUp(self) -> None:
        reset_store()

    def test_api_flow(self) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(fastapi_app)
        self.assertEqual(client.get("/api/rendertask").status_code, 200)
        self.assertEqual(client.get("/api/worker/schedulable").status_code, 200)
        self.assertEqual(client.get("/api/dispatch/plan").status_code, 200)

        reap = client.post("/api/rendertask/reap")
        self.assertEqual(reap.status_code, 200)
        self.assertGreaterEqual(reap.json()["已回收"], 1)

        results = client.get("/api/rendertask/2/results")
        self.assertEqual(results.status_code, 200)
        self.assertTrue(results.json()["items"])

        sync = client.post("/api/dispatch/sync")
        self.assertEqual(sync.status_code, 200)
        self.assertTrue(sync.json()["manifest"]["已消费结果版本"] >= 1)


if __name__ == "__main__":
    unittest.main()

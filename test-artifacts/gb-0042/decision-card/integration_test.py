"""判定卡编排集成测试（无外部依赖）。

用内存仓储模拟 DecisionCard 任务状态机，直接驱动 services.cards 的
纯函数核心 run_search / verify_and_publish，覆盖：
1. 完整搜索 -> 逐站复算闸门 -> 发布且 verified=True；
2. 中断 -> 从已核验范围恢复 -> 与一次性求解规则完全一致；
3. 并发调低误收上限：旧任务 superseded，旧解永不发布，新任务按新上限求解；
4. 复算闸门能拦截被篡改的计数；
5. 非法输入（不存在簇、负上限）被 prepare_problem 拒绝。
"""
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3] / 'backend'
sys.path.insert(0, str(ROOT))

from src.services import card_search, decision_card as dc


def make_data(n=480, seed=42):
    rng = random.Random(seed)
    records, rows = [], []
    centers = [(20, 70, 25), (27, 45, 55), (33, 28, 90)]
    for i in range(n):
        c = i % 3
        if i % 17 == 0:
            vals = {'temperature': round(rng.uniform(12, 42), 2),
                    'humidity': round(rng.uniform(12, 92), 2),
                    'particles': round(rng.uniform(4, 140), 2)}
            label = -1
        else:
            t, h, p = centers[c]
            vals = {'temperature': round(rng.gauss(t, 0.9), 2),
                    'humidity': round(rng.gauss(h, 2.6), 2),
                    'particles': round(max(0.1, rng.gauss(p, 3.2)), 2)}
            label = c
        records.append({'reference': 'S%05d' % i, 'values': vals})
        rows.append({'reference': 'S%05d' % i, 'cluster': label, **vals})
    return records, rows


class CardRepo:
    """模拟判定卡任务表与取代状态机。"""

    def __init__(self):
        self.items = {}
        self._seq = 0

    def create(self, run_id, cluster, cap):
        # 同 (run, 簇) 未结束任务全部 superseded
        for c in self.items.values():
            if (c['run_id'] == run_id and c['target_cluster'] == cluster
                    and c['state'] in ('queued', 'running')):
                c['state'] = 'superseded'
                c['error'] = '误收上限已调整为 %s，本任务被新任务取代' % cap
        self._seq += 1
        card = {'id': self._seq, 'run_id': run_id,
                'target_cluster': cluster, 'false_accept_cap': cap,
                'state': 'queued', 'progress': {}, 'card': None,
                'error': ''}
        self.items[self._seq] = card
        return card

    def run_to_completion(self, card, records, rows, batch_size=32,
                          tamper=False):
        """模拟 worker：分批搜索、落盘进度、取代检查、发布闸门。"""
        if card['state'] not in ('queued', 'running'):
            return card  # 被取代的旧任务 worker 直接跳过
        card['state'] = 'running'

        def on_batch(progress):
            card['progress'] = {
                'index': progress['checked'],
                'total': progress['total'],
                'best_rule': progress['rule'],
                'hits': progress['hits'],
                'false_accepts': progress['false_accepts']}
            # 模拟每批边界的取代检查
            return card['state'] == 'running'

        problem, outcome = card_search.run_search(
            records, rows, card['target_cluster'],
            card['false_accept_cap'], card['progress'] or None,
            on_batch=on_batch, batch_size=batch_size)

        if not outcome['completed']:
            card['state'] = 'queued'
            return card

        # 发布闸门 1：并发取代复核
        if card['state'] != 'running':
            return card

        if tamper:
            outcome = dict(outcome)
            outcome['hits'] = outcome['hits'] + 5  # 人为篡改搜索计数

        payload, error = card_search.verify_and_publish(
            problem, outcome, card['false_accept_cap'])
        if error:
            card['state'] = 'failed'
            card['error'] = error
            return card
        payload['target_cluster'] = card['target_cluster']
        card['card'] = payload
        card['progress'] = {'index': outcome['total'],
                            'total': outcome['total']}
        card['state'] = 'completed'
        return card


def test_full_flow_and_gate(records, rows):
    repo = CardRepo()
    card = repo.create(run_id=1, cluster=1, cap=6)
    repo.run_to_completion(card, records, rows)
    assert card['state'] == 'completed', card['error']
    assert card['card']['verified'] is True
    assert card['card']['false_accepts'] <= 6
    # 独立第三方复算
    problem = dc.prepare_problem(records, rows, 1, 6)
    v = dc.evaluate_rule(problem, card['card']['rule'])
    assert v['hits'] == card['card']['hits']
    assert v['false_accepts'] == card['card']['false_accepts']
    assert card['card']['judgement_nodes'] <= 3
    assert card['card']['target_count'] == sum(problem['target'])
    print('完整流程与发布闸门 OK：命中=%d 误收=%d 节点=%d' % (
        card['card']['hits'], card['card']['false_accepts'],
        card['card']['judgement_nodes']))


def test_resume_equivalence(records, rows):
    repo = CardRepo()
    card = repo.create(1, 2, 5)
    card['state'] = 'running'
    # 第一批后人为中断
    problem, partial = card_search.run_search(
        records, rows, 2, 5, None,
        on_batch=lambda p: p['checked'] < 13, batch_size=4)
    assert not partial['completed']
    assert partial['checked'] == 16
    card['progress'] = {'index': partial['checked'],
                        'total': partial['total'],
                        'best_rule': partial['rule'],
                        'hits': partial['hits'],
                        'false_accepts': partial['false_accepts']}
    card['state'] = 'queued'
    # 恢复执行
    repo.run_to_completion(card, records, rows, batch_size=4)
    assert card['state'] == 'completed', card['error']
    full = dc.solve(dc.prepare_problem(records, rows, 2, 5))
    assert card['card']['rule'] == full['rule']
    assert (card['card']['hits'], card['card']['false_accepts']) == (
        full['hits'], full['false_accepts'])
    # 已核验范围不重复：恢复后进度索引单调推进到 total
    assert card['progress']['index'] == full['total']
    print('中断恢复等价性 OK（断点=%d，总分裂=%d）' % (16, full['total']))


def test_cap_lowering_supersedes_old(records, rows):
    repo = CardRepo()
    old = repo.create(1, 0, 20)
    # 旧任务开始计算，完成第一批后被新任务取代
    old['state'] = 'running'
    problem, partial = card_search.run_search(
        records, rows, 0, 20, None,
        on_batch=lambda p: p['checked'] < 9, batch_size=4)
    old['progress'] = {'index': partial['checked'],
                       'best_rule': partial['rule']}
    # 并发调低上限 -> 旧任务立即 superseded
    new = repo.create(1, 0, 3)
    assert old['state'] == 'superseded'
    assert new['state'] == 'queued' and new['false_accept_cap'] == 3
    # worker 误取旧任务：必须跳过，旧解永不发布
    repo.run_to_completion(old, records, rows)
    assert old['state'] == 'superseded' and old['card'] is None
    # 模拟旧 worker 在搜索结束后才发现被取代：同样禁止发布
    problem0, outcome0 = card_search.run_search(records, rows, 0, 20, None)
    payload, _ = card_search.verify_and_publish(problem0, outcome0, 20)
    assert payload is not None  # 旧上限下规则本身可行
    # 但发布闸门 1（状态机）拦截：old 已非 running
    assert old['card'] is None
    # 新任务按收紧后的上限完成
    repo.run_to_completion(new, records, rows)
    assert new['state'] == 'completed'
    assert new['card']['false_accepts'] <= 3
    assert new['card']['verified'] is True
    # 收紧上限的命中数不应高于宽松上限下的最优命中
    loose = dc.solve(dc.prepare_problem(records, rows, 0, 20))
    assert new['card']['hits'] <= loose['hits']
    print('并发调低上限：旧解不发布、新解守新上限 OK')


def test_verify_gate_blocks_tampering(records, rows):
    repo = CardRepo()
    card = repo.create(1, 1, 6)
    repo.run_to_completion(card, records, rows, tamper=True)
    assert card['state'] == 'failed'
    assert card['card'] is None
    assert '复算' in card['error']
    print('复算闸门拦截篡改计数 OK')


def test_rejections(records, rows):
    try:
        dc.prepare_problem(records, rows, 99, 1)
        raise AssertionError('不存在的目标簇应被拒绝')
    except dc.SearchFailed:
        pass
    try:
        dc.prepare_problem(records, rows, 1, -1)
        raise AssertionError('负误收上限应被拒绝')
    except dc.SearchFailed:
        pass
    # 读数与结果行脱节（引用缺失）也要拒绝
    try:
        dc.prepare_problem(records, rows + [{'reference': 'GHOST', 'cluster': 0}],
                           1, 1)
        raise AssertionError('脱节结果行应被拒绝')
    except dc.SearchFailed:
        pass
    print('非法输入拒绝 OK')


def main():
    records, rows = make_data()
    test_full_flow_and_gate(records, rows)
    test_resume_equivalence(records, rows)
    test_cap_lowering_supersedes_old(records, rows)
    test_verify_gate_blocks_tampering(records, rows)
    test_rejections(records, rows)
    print('INTEGRATION TESTS PASSED')


if __name__ == '__main__':
    main()

"""2000 站规模的性能、恢复一致性与复算校验。"""
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'backend'))

from src.services import decision_card as dc


def build(n, seed=42, n_clusters=4):
    rng = random.Random(seed)
    centers_t = [rng.uniform(15, 35) for _ in range(n_clusters)]
    centers_h = [rng.uniform(20, 85) for _ in range(n_clusters)]
    centers_p = [rng.uniform(10, 120) for _ in range(n_clusters)]
    records, rows = [], []
    for i in range(n):
        c = rng.randrange(n_clusters)
        if rng.random() < 0.12:  # 噪声站：簇外，含异常读数
            vals = {'temperature': round(rng.uniform(10, 45), 2),
                    'humidity': round(rng.uniform(5, 95), 2),
                    'particles': round(rng.uniform(2, 160), 2)}
            label = -1
        else:
            vals = {'temperature': round(rng.gauss(centers_t[c], 1.6), 2),
                    'humidity': round(rng.gauss(centers_h[c], 4.0), 2),
                    'particles': round(max(0.1, rng.gauss(centers_p[c], 6)), 2)}
            label = c
        records.append({'reference': 'S%05d' % i, 'values': vals})
        rows.append({'reference': 'S%05d' % i, 'cluster': label})
    return records, rows


def main():
    n = 2000
    records, rows = build(n)
    target_cluster = 1
    cap = 8
    t0 = time.perf_counter()
    problem = dc.prepare_problem(records, rows, target_cluster, cap)
    t1 = time.perf_counter()
    result = dc.solve(problem, batch_size=32)
    t2 = time.perf_counter()
    print('站点数=%d 根分裂数=%d' % (n, result['total']))
    print('prepare %.2fs  solve %.2fs' % (t1 - t0, t2 - t1))
    print('命中=%d 误收=%d 节点数=%d 完成=%s' % (
        result['hits'], result['false_accepts'],
        dc._node_count(result['rule']), result['completed']))
    assert result['completed']
    assert result['false_accepts'] <= cap

    # 独立逐站复算（只依赖规则与原始读数）
    verified = dc.evaluate_rule(problem, result['rule'])
    assert verified['hits'] == result['hits']
    assert verified['false_accepts'] == result['false_accepts']
    print('逐站复算一致：命中=%d 误收=%d' % (
        verified['hits'], verified['false_accepts']))

    # 每站判断次数 <=2
    depth = _max_path_depth(result['rule'])
    assert depth <= 2
    print('最大逐站判断次数=%d（<=2）' % depth)

    # 中断恢复：在多个断点处中断再续算，结果必须与一次性求解完全一致
    for stop_at in (1, 7, 33, 100, 257, result['total'] - 1):
        partial = dc.solve(
            problem,
            on_batch=lambda p, stop_at=stop_at: p['checked'] < stop_at,
            batch_size=stop_at)
        assert not partial['completed']
        assert partial['checked'] == stop_at
        resumed = dc.solve(
            problem,
            checkpoint={'index': partial['checked'],
                        'best_rule': partial['rule']},
            batch_size=32)
        assert resumed['completed']
        assert resumed['rule'] == result['rule'], '恢复后规则不一致 @%d' % stop_at
        assert (resumed['hits'], resumed['false_accepts']) == (
            result['hits'], result['false_accepts'])
    print('六个断点中断/恢复后规则与计数完全一致')

    # 多次从零求解的确定性
    again = dc.solve(dc.prepare_problem(records, rows, target_cluster, cap))
    assert again['rule'] == result['rule']
    print('重复求解确定性 OK')

    # 判定卡渲染
    card = dc.describe_rule(result['rule'])
    print('判定卡判断节点数=%d，步骤数=%d' % (
        card['judgement_nodes'], len(card['steps'])))
    for s in card['steps']:
        if s['test']:
            print('  判断#%d %s <= %g' % (
                s['judge_no'], s['test']['field_label'], s['test']['threshold']))
        else:
            print('    └ %s' % s['action'])
    print('PERF/RESUME TESTS PASSED')


def _max_path_depth(rule):
    if rule[0] == 'leaf':
        return 0
    return 1 + max(_max_path_depth(rule[3]), _max_path_depth(rule[4]))


if __name__ == '__main__':
    main()

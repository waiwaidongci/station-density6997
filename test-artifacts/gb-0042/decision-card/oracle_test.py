"""暴力枚举 oracle：穷举全部深度<=2判定树，对照精确求解器。

仅用于测试（纯标准库），不参与线上计算。穷举规模 O((3n)^3)，
只在 n<=14 的随机小例上运行。
"""
import itertools
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'backend'))

from src.services import decision_card as dc

FEATURES = dc.FEATURES


def brute_thresholds(values, n):
    out = {}
    for f in FEATURES:
        vals = sorted(set(values[f]))
        out[f] = vals[:-1]  # 末阈值右支为空，求解器同样跳过
    return out


def gen_trees(thr, depth):
    # 叶规则：决策 0/1
    yield ('leaf', 0)
    yield ('leaf', 1)
    if depth == 0:
        return
    for fi, f in enumerate(FEATURES):
        for t in thr[f]:
            for left in gen_trees(thr, depth - 1):
                for right in gen_trees(thr, depth - 1):
                    # 规范化：两个子树完全相同的分裂等价于叶规则，跳过
                    if left == right:
                        continue
                    yield ('split', fi, t, left, right)


def predict(rule, values, i):
    if rule[0] == 'leaf':
        return rule[1]
    f = FEATURES[rule[1]]
    if values[f][i] <= rule[2]:
        return predict(rule[3], values, i)
    return predict(rule[4], values, i)


def metrics(rule, values, target, n):
    hits = fa = 0
    for i in range(n):
        if predict(rule, values, i) == 1:
            if target[i]:
                hits += 1
            else:
                fa += 1
    return hits, fa


def make_problem(rng, n, n_clusters=2):
    values = {f: [round(rng.uniform(0, 100), 1) for _ in range(n)]
              for f in FEATURES}
    labels = [rng.randrange(-1, n_clusters) for _ in range(n)]
    target_cluster = rng.randrange(n_clusters)
    # 保证目标簇至少出现一次（prepare_problem 对不存在簇明确拒绝）
    if target_cluster not in labels:
        labels[rng.randrange(n)] = target_cluster
    records = [{'reference': 'S%04d' % i,
                'values': {f: values[f][i] for f in FEATURES}}
               for i in range(n)]
    rows = [{'reference': 'S%04d' % i, 'cluster': labels[i]} for i in range(n)]
    problem = dc.prepare_problem(records, rows, target_cluster, 0)
    # prepare_problem 是标签的唯一权威口径，暴力对照必须使用同一份 target
    target = problem['target']
    return problem, values, target


def run_case(seed, n, cap):
    rng = random.Random(seed)
    problem, values, target = make_problem(rng, n)
    problem['false_accept_cap'] = cap
    # 暴力穷举。择优口径与业务一致：命中最大；同命中取误收更小；
    # 命中与误收都相同再按固定次序（节点数 -> 字段 -> 阈值 -> 分支）。
    thr = brute_thresholds(values, n)
    best = None
    for rule in gen_trees(thr, 2):
        h, fa = metrics(rule, values, target, n)
        if fa > cap:
            continue
        key = dc._rule_order_key(rule)
        cand = (h, fa, key)
        if best is None or h > best[0] or (h == best[0] and (
                fa < best[1] or (fa == best[1] and key < best[2]))):
            best = (h, fa, key, rule)
    got = dc.solve(problem)
    got_rule = got['rule']
    gh, gfa = metrics(got_rule, values, target, n)
    assert (gh, gfa) == (got['hits'], got['false_accepts']), \
        '求解器自报计数与独立预测不一致 seed=%s' % seed
    assert gh == best[0] and gfa == best[1], \
        'seed=%s cap=%s 求解器(命中%s,误收%s) != 暴力(命中%s,误收%s)' % (
            seed, cap, gh, gfa, best[0], best[1])
    assert dc._rule_order_key(got_rule) == best[2], \
        'seed=%s 同分次序不一致' % seed
    # 独立复算口径一致
    verified = dc.evaluate_rule(problem, got_rule)
    assert (verified['hits'], verified['false_accepts']) == (gh, gfa)
    # 节点数：深度<=2 的树最多 3 个判断节点，任一站点路径上最多判断 2 次
    assert dc._node_count(got_rule) <= 3
    assert _max_depth(got_rule) <= 2
    return gh, gfa, dc._node_count(got_rule)


def _max_depth(rule):
    if rule[0] == 'leaf':
        return 0
    return 1 + max(_max_depth(rule[3]), _max_depth(rule[4]))


def test_identical_readings_same_side():
    # 同读数同侧：对规则中每个判断节点，该字段读数相等的站点必须走同一支。
    n = 12
    rng = random.Random(7)
    problem, values, target = make_problem(rng, n)
    # 强制温度列恒定，覆盖“整列同值无法分裂”的退化情形
    values['temperature'] = [25.0] * n
    records = [{'reference': 'S%04d' % i,
                'values': {f: values[f][i] for f in FEATURES}}
               for i in range(n)]
    rows = [{'reference': 'S%04d' % i,
             'cluster': problem['target_cluster'] if target[i] else -1}
            for i in range(n)]
    p2 = dc.prepare_problem(records, rows, problem['target_cluster'], 99)
    rule = dc.solve(p2)['rule']

    def collect_thresholds(node, acc):
        if node[0] == 'leaf':
            return
        acc.setdefault(FEATURES[node[1]], set()).add(node[2])
        collect_thresholds(node[3], acc)
        collect_thresholds(node[4], acc)

    thresholds = {}
    collect_thresholds(rule, thresholds)
    # 恒定温度列上不允许出现温度阈值
    assert 'temperature' not in thresholds
    for field, ts in thresholds.items():
        col = values[field]
        for t in ts:
            # 阈值必须落在两个不同实际读数之间，等值读数整体在同一侧
            equal_groups = {}
            for i in range(n):
                equal_groups.setdefault(col[i], set()).add(
                    col[i] <= t)
            assert all(len(side) == 1 for side in equal_groups.values())
    # 预测仍逐站与独立复算一致
    preds = dc.evaluate_rule(p2, rule)['predictions']
    assert all(p in (0, 1) for p in preds)
    print('同读数同侧约束 OK')


def test_tie_break_prefers_fewer_nodes():
    # 全目标可被单分裂完美分开时，深度2树同分也必须选深度1
    n = 10
    values = {f: [float(i) for i in range(n)] for f in FEATURES}
    labels = [0 if i < 6 else 1 for i in range(n)]
    records = [{'reference': 'S%04d' % i,
                'values': {f: values[f][i] for f in FEATURES}}
               for i in range(n)]
    rows = [{'reference': 'S%04d' % i, 'cluster': labels[i]} for i in range(n)]
    problem = dc.prepare_problem(records, rows, 0, 0)
    got = dc.solve(problem)
    assert got['hits'] == 6 and got['false_accepts'] == 0
    assert dc._node_count(got['rule']) == 1, '同分应取节点更少的规则'
    # 次序固定：温度字段序最靠前
    assert got['rule'][1] == 0
    print('节点少优先与字段次序 OK')


def test_cap_zero_no_feasible_split():
    # cap=0 且目标与非目标完全重叠：只能全部放行
    n = 8
    values = {f: [1.0, 1.0, 2.0, 2.0, 3.0, 3.0, 4.0, 4.0] for f in FEATURES}
    labels = [0, -1, 0, -1, 0, -1, 0, -1]
    records = [{'reference': 'S%04d' % i,
                'values': {f: values[f][i] for f in FEATURES}}
               for i in range(n)]
    rows = [{'reference': 'S%04d' % i, 'cluster': labels[i]} for i in range(n)]
    problem = dc.prepare_problem(records, rows, 0, 0)
    got = dc.solve(problem)
    assert got['hits'] == 0 and got['false_accepts'] == 0
    assert got['rule'][0] == 'leaf'
    print('零上限无可分规则时回退全放行 OK')


def test_rendered_tree_matches_rule():
    # 渲染给前端的嵌套树按阈值比较做预测，必须与内部规则逐站一致
    rng = random.Random(31)
    problem, values, target = make_problem(rng, 14)
    problem['false_accept_cap'] = 2
    rule = dc.solve(problem)['rule']
    tree = dc.describe_rule(rule)['tree']

    def predict_tree(node, i):
        if node['type'] == 'leaf':
            return 1 if node['decision'] == 'visit' else 0
        f = node['field']
        if values[f][i] <= node['threshold']:
            return predict_tree(node['left'], i)
        return predict_tree(node['right'], i)

    internal = dc.evaluate_rule(problem, rule)['predictions']
    rendered = [predict_tree(tree, i) for i in range(14)]
    assert internal == rendered
    print('渲染树与内部规则预测一致 OK')


def main():
    cases = 0
    for seed in range(120):
        n = random.Random(seed).choice((6, 8, 10, 12, 14))
        cap = random.Random(seed + 999).choice((0, 1, 2, 3, 5, 9))
        h, fa, nodes = run_case(seed, n, cap)
        cases += 1
    print('随机小例暴力对照：%d 例全部一致（命中/误收/同分次序）' % cases)
    test_identical_readings_same_side()
    test_tie_break_prefers_fewer_nodes()
    test_cap_zero_no_feasible_split()
    test_rendered_tree_matches_rule()
    print('ALL ORACLE TESTS PASSED')


if __name__ == '__main__':
    main()

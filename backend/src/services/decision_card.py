# 复访判定卡精确求解器（纯标准库，无第三方依赖）。
#
# 业务口径：
# - 输入是一次已冻结研究的站点读数（温度/湿度/颗粒三项原始读数）与 DBSCAN 分区标签。
# - 目标簇站点命中为收益；簇外站点（含噪声标签 -1）被规则判中即误收。
# - 规则是深度不超过 2 的判定树：每个内部节点只比较一项原始读数与阈值，
#   x <= 阈值走左支，x > 阈值走右支；同读数恒在同侧。
# - 目标：在误收不超过上限的前提下最大化命中；同分（命中、误收相同）先取
#   判断节点更少的，再按 (字段, 阈值, 分支) 的固定顺序取首。
#
# 精确性与可扩展性：
# - 枚举所有根分裂（至多 3*(n-1) 个），每个子集只保留按误收计数的 Pareto
#   前沿（同一误收数只留命中最高且次序最小的局部规则），不枚举完整判定树、
#   不保存全部候选。深度 2 树的最优性由两个子集前沿的精确合并保证。
# - 搜索以根分裂为最小恢复单位，断点持久化后可从已核验范围之后继续。

FEATURES = ('temperature', 'humidity', 'particles')
# 字段固定顺序：温度、湿度、颗粒；分支固定顺序：左支(x<=t)先于右支(x>t)。
FIELD_ORDER = {name: i for i, name in enumerate(FEATURES)}


class SearchFailed(Exception):
    """输入不满足求解前提。"""


def _leaf_rule(hits, fa):
    # 规则元组：('leaf', 左支决策, 命中, 误收)。决策 1=判中（建议复访），0=放行。
    return ('leaf', 1 if hits else 0, hits, fa)


def _split_rule(field, value, left_rule, right_rule, hits, fa):
    # 规则元组：('split', 字段序号, 阈值, 左规则, 右规则, 命中, 误收)。
    return ('split', FIELD_ORDER[field], value, left_rule, right_rule, hits, fa)


def _rule_metrics(rule):
    return rule[-2], rule[-1]


def _better(hits, fa, rule, best):
    """全局择优：命中最大；同命中误收更小；再同则按固定次序（节点少优先）。"""
    if best is None:
        return True
    if hits != best[0]:
        return hits > best[0]
    if fa != best[1]:
        return fa < best[1]
    return _rule_order_key(rule) < best[2]


def _rule_order_key(rule):
    # 同分比较用的固定次序。
    # 第一优先：判断节点数（少者优先，leaf=0，单分裂=1，深树=2/3）；
    # 节点数相同再按字段序号、阈值、左支结构、右支结构的字典序，
    # 分支固定左先右后。元组递归比较天然实现该顺序。
    return (_node_count(rule), tuple(_order_tokens(rule)))


def _order_tokens(rule):
    if rule[0] == 'leaf':
        return (0, rule[1])
    return (1, rule[1], rule[2], tuple(_order_tokens(rule[3])),
            tuple(_order_tokens(rule[4])))


def _node_count(rule):
    if rule[0] == 'leaf':
        return 0
    return 1 + _node_count(rule[3]) + _node_count(rule[4])


def prepare_problem(records, rows, target_cluster, false_accept_cap):
    """校验并构造求解问题。返回不可变问题描述，供搜索与独立复算共用。"""
    if not isinstance(target_cluster, int):
        raise SearchFailed('目标簇编号必须是整数')
    if not isinstance(false_accept_cap, int) or false_accept_cap < 0:
        raise SearchFailed('误收上限必须是非负整数')
    by_ref = {r['reference']: r for r in records}
    n = len(rows)
    if n < 2:
        raise SearchFailed('冻结站点不足，无法构造判定规则')
    target = bytearray(n)
    values = {f: [0.0] * n for f in FEATURES}
    cluster_seen = False
    for i, row in enumerate(rows):
        ref = row.get('reference')
        rec = by_ref.get(ref)
        if rec is None:
            raise SearchFailed('结果行与冻结读数不一致：缺少 %s' % ref)
        vals = rec['values']
        for f in FEATURES:
            v = vals.get(f)
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise SearchFailed('读数 %s 不是数值：%s' % (f, ref))
            v = float(v)
            if v != v or v in (float('inf'), float('-inf')):
                raise SearchFailed('读数 %s 非有限数值：%s' % (f, ref))
            values[f][i] = v
        label = row.get('cluster')
        if not isinstance(label, int):
            raise SearchFailed('分区标签缺失或非整数：%s' % ref)
        if label == target_cluster:
            target[i] = 1
            cluster_seen = True
    if not cluster_seen:
        raise SearchFailed('目标簇 %s 在本次冻结研究中不存在' % target_cluster)
    order = {}
    distinct = {}
    for f in FEATURES:
        col = values[f]
        idx = sorted(range(n), key=lambda j, col=col: (col[j], j))
        order[f] = idx
        # 每个字段的去重读数，按 (字段顺序, 值) 固定排列。
        seen = []
        last = None
        for j in idx:
            v = col[j]
            if last is None or v != last:
                seen.append(v)
                last = v
        distinct[f] = seen
    return {
        'n': n,
        'target': bytes(target),
        'values': values,
        'order': order,
        'distinct': distinct,
        'target_cluster': target_cluster,
        'false_accept_cap': false_accept_cap,
        'references': [r['reference'] for r in rows],
    }


def _depth1_frontier(problem, members, cap):
    """对给定站点子集求深度 <=1 规则的命中前沿。

    返回按命中数升序的列表 [(hits, fa, rule), ...]：每个可达命中数只保留
    误收最小（同分取次序最小）的一条规则，不做跨命中支配剪枝。命中数上界
    为子集目标站数（不超过总站点数），规模有保证，且不保存全部候选。
    """
    target = problem['target']
    total_t = sum(1 for i in members if target[i])
    total_o = len(members) - total_t
    best_by_hits = {}

    def add(rule):
        hits, fa = _rule_metrics(rule)
        if fa > cap:
            return
        old = best_by_hits.get(hits)
        if old is None or fa < old[0] or (fa == old[0]
                                          and _rule_order_key(rule) < _rule_order_key(old[1])):
            best_by_hits[hits] = (fa, rule)

    # 深度0叶规则：全放行 (0,0)；全判中 (total_t, total_o) 受预算截断。
    add(_leaf_rule(0, 0))
    if total_o <= cap:
        add(_leaf_rule(total_t, total_o))

    for field in FEATURES:
        col = problem['values'][field]
        # 严格按子集自身的字段顺序扫描，等值读数整段同入左支，
        # 不依赖全局排序中的段位置（子集在全局序列里可能不连续）。
        ordered = sorted(members, key=lambda j, col=col: (col[j], j))
        lt = lo = 0
        pos = 0
        m = len(ordered)
        while pos < m:
            v = col[ordered[pos]]
            seg_t = seg_o = 0
            while pos < m and col[ordered[pos]] == v:
                if target[ordered[pos]]:
                    seg_t += 1
                else:
                    seg_o += 1
                pos += 1
            lt += seg_t
            lo += seg_o
            if pos == m:
                break  # 末阈值右支为空，与叶规则等价，跳过
            rt, ro = total_t - lt, total_o - lo
            # 每侧叶子有两个候选：放行 (0,0) 或判中 (本侧全部命中, 本侧误收)。
            left_choices = [_leaf_rule(0, 0)]
            if lt:
                left_choices.append(_leaf_rule(lt, lo))
            right_choices = [_leaf_rule(0, 0)]
            if rt:
                right_choices.append(_leaf_rule(rt, ro))
            for left_pick in left_choices:
                for right_pick in right_choices:
                    if left_pick == right_pick:
                        continue  # 两子树相同的分裂等价于叶规则，节点更多，跳过
                    lh, lf = _rule_metrics(left_pick)
                    rh, rf = _rule_metrics(right_pick)
                    add(_split_rule(field, v, left_pick, right_pick,
                                    lh + rh, lf + rf))
    return _prune_hits(best_by_hits)


def _prune_hits(best_by_hits):
    """命中前沿：每个可达命中数保留误收最小（同分取次序最小）的一条规则。

    不做跨命中数的支配剪枝——不同命中数的规则在深度2合并中对应不同的
    总命中/总误收搭配，删除任一点都可能破坏最优性。命中数上界是子集内
    目标站数（不超过总站点数），因此前沿规模有保证，无需保存全部候选。
    """
    return [(hits, best_by_hits[hits][0], best_by_hits[hits][1])
            for hits in sorted(best_by_hits)]


def _combine(frontier_l, frontier_r, cap, root_field, root_value):
    """两个深度<=1命中前沿的精确合并，求该根分裂下深度<=2树的最优规则。

    两侧均按命中升序；对左支每个选择，在右支预算允许的点中取命中最高者
    （同命中取误收最小、次序最小），无需枚举全部完整树。
    """
    best = None  # (hits, fa, order_key, rule)

    def consider(hits, fa, rule):
        if fa > cap:
            return
        nonlocal best
        if _better(hits, fa, rule,
                   None if best is None else (best[0], best[1], best[2])):
            best = (hits, fa, _rule_order_key(rule), rule)

    for h_l, fa_l, rule_l in frontier_l:
        if fa_l > cap:
            continue
        budget = cap - fa_l
        # 在右支全部可行点中取预算内命中最高者；同命中取误收最小、次序最小。
        best_r = None
        for h_r, fa_r, rule_r in frontier_r:
            if fa_r <= budget and (
                    best_r is None or h_r > best_r[0]
                    or (h_r == best_r[0]
                        and (fa_r < best_r[1]
                             or (fa_r == best_r[1]
                                 and _rule_order_key(rule_r) < _rule_order_key(best_r[2]))))):
                best_r = (h_r, fa_r, rule_r)
        if best_r is not None:
            h_r, fa_r, rule_r = best_r
            merged = _split_rule(root_field, root_value, rule_l, rule_r,
                                 h_l + h_r, fa_l + fa_r)
            consider(h_l + h_r, fa_l + fa_r, merged)
    return best


def _build_root_splits(problem):
    """一次性构造全部根分裂，按 (字段, 阈值) 固定顺序排列。

    每项 (field, value, left_members, left_set)；沿字段排序单趟累积，
    避免恢复时对每个分裂重复全表扫描。末阈值右支为空，跳过。
    """
    n = problem['n']
    splits = []
    for field in FEATURES:
        col = problem['values'][field]
        order = problem['order'][field]
        members = []
        last = None
        for j in order:
            v = col[j]
            if last is not None and v != last:
                # 成员列表此刻已确定，再构造独立的成员标记快照。
                member_set = bytearray(n)
                for m in members:
                    member_set[m] = 1
                splits.append((field, last, list(members), member_set))
            members.append(j)
            last = v
    return splits


def root_split_count(problem):
    return sum(len(problem['distinct'][f]) - 1 for f in FEATURES)


def _complement(problem, left_set):
    n = problem['n']
    right_set = bytearray(n)
    right_members = []
    for j in range(n):
        if not left_set[j]:
            right_set[j] = 1
            right_members.append(j)
    return right_members, right_set


def solve(problem, checkpoint=None, on_batch=None, batch_size=24):
    """执行精确搜索。

    checkpoint：上次中断时持久化的进度 {'index': k, 'best_rule': rule}。
    on_batch：每个批次边界调用的回调，入参为当前进度快照，返回 False 时
    在该批次已核验范围落盘后干净中断（已核验范围不会重复计算）。
    返回 {'rule', 'hits', 'false_accepts', 'checked', 'total', 'completed'}。
    """
    cap = problem['false_accept_cap']
    n = problem['n']
    target = problem['target']
    total_t = sum(target)

    # 深度 0 基线：全放行叶规则永远可行（0 命中 0 误收，节点最少）；
    # 全判中叶规则仅在误收满足预算时参与。
    pass_leaf = _leaf_rule(0, 0)
    best = (0, 0, _rule_order_key(pass_leaf), pass_leaf)
    base_visit = _leaf_rule(total_t, n - total_t)
    if base_visit[-1] <= cap and _better(
            base_visit[-2], base_visit[-1], base_visit, best[:3]):
        best = (base_visit[-2], base_visit[-1],
                _rule_order_key(base_visit), base_visit)

    total = root_split_count(problem)
    splits = _build_root_splits(problem)
    start = 0
    if checkpoint:
        start = min(int(checkpoint.get('index', 0)), total)
        saved = checkpoint.get('best_rule')
        if saved is not None and saved[-1] <= cap and _better(
                saved[-2], saved[-1], saved, best[:3]):
            best = (saved[-2], saved[-1], _rule_order_key(saved), saved)

    index = start
    while index < total:
        end = min(index + batch_size, total)
        for k in range(index, end):
            field, value, left_members, left_set = splits[k]
            right_members, _right_set = _complement(problem, left_set)
            fl = _depth1_frontier(problem, left_members, cap)
            fr = _depth1_frontier(problem, right_members, cap)
            cand = _combine(fl, fr, cap, field, value)
            if cand is not None and _better(cand[0], cand[1], cand[3], best[:3]):
                best = cand
        index = end
        progress = {
            'rule': best[3], 'hits': best[0],
            'false_accepts': best[1],
            'checked': index, 'total': total, 'completed': False}
        if on_batch is not None and on_batch(progress) is False:
            return progress
    return {
        'rule': best[3],
        'hits': best[0],
        'false_accepts': best[1],
        'checked': total,
        'total': total,
        'completed': True,
    }


def evaluate_rule(problem, rule):
    """独立逐站复算：不引用搜索过程中的任何计数，只按规则与原始读数判定。"""
    values = problem['values']
    target = problem['target']
    hits = fa = 0
    predictions = []
    for i in range(problem['n']):
        pred = _predict(rule, values, i)
        predictions.append(pred)
        if pred == 1:
            if target[i]:
                hits += 1
            else:
                fa += 1
    return {'hits': hits, 'false_accepts': fa, 'predictions': predictions}


def _predict(rule, values, i):
    if rule[0] == 'leaf':
        return rule[1]
    field = FEATURES[rule[1]]
    if values[field][i] <= rule[2]:
        return _predict(rule[3], values, i)
    return _predict(rule[4], values, i)


def describe_rule(rule):
    """把内部规则元组渲染成外业可直接照读的判定卡。

    返回：
    - tree：嵌套判定树，前端递归渲染，叶节点给出判中/放行动作；
    - steps：按先根遍历压平的线性步骤，供打印与审计；
    - judgement_nodes：判断节点总数；
    - max_judgements_per_station：任一站点路径上的最多判断次数（<=2）。
    """
    counter = {'no': 0}

    def build(node):
        if node[0] == 'leaf':
            if node[1] == 1:
                return {'type': 'leaf', 'decision': 'visit',
                        'action': '判中：列入复访'}
            return {'type': 'leaf', 'decision': 'pass',
                    'action': '放行：不列入复访'}
        field = FEATURES[node[1]]
        counter['no'] += 1
        return {
            'type': 'judge',
            'judge_no': counter['no'],
            'field': field,
            'field_label': _field_label(field),
            'threshold': node[2],
            'threshold_text': _fmt(node[2]),
            'left_text': '读数 ≤ %s' % _fmt(node[2]),
            'right_text': '读数 > %s' % _fmt(node[2]),
            'left': build(node[3]),
            'right': build(node[4])}

    tree = build(rule)
    steps = _flatten(tree)
    return {'tree': tree, 'steps': steps,
            'judgement_nodes': _node_count(rule),
            'max_judgements_per_station': _max_depth(rule)}


def _flatten(node, depth=0, branch='root'):
    if node['type'] == 'leaf':
        return [{'depth': depth, 'branch': branch, 'judge_no': None,
                 'action': node['action'], 'test': None}]
    out = [{'depth': depth, 'branch': branch, 'judge_no': node['judge_no'],
            'action': None,
            'test': {'field': node['field'],
                     'field_label': node['field_label'],
                     'threshold': node['threshold'],
                     'left': node['left_text'], 'right': node['right_text']}}]
    out += _flatten(node['left'], depth + 1, 'left')
    out += _flatten(node['right'], depth + 1, 'right')
    return out


def _max_depth(rule):
    if rule[0] == 'leaf':
        return 0
    return 1 + max(_max_depth(rule[3]), _max_depth(rule[4]))


def _field_label(field):
    return {'temperature': '温度 ℃', 'humidity': '相对湿度 %',
            'particles': '颗粒浓度 μg/m³'}[field]


def _fmt(v):
    return ('%g' % v)

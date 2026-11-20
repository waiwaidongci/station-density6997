"""判定卡搜索的纯函数核心：无 ORM/框架依赖，worker 与测试共用。"""
from . import decision_card as dc

# 队列名与批大小在此定义，由 services.cards / worker 导入复用。
QUEUE_KEY = 'decision-card:queue'
BATCH_SIZE = 32

# ---------------------------------------------------------------------------
# 纯函数核心：不依赖 ORM，输入/输出均为普通字典，供 worker 与测试共用。
# ---------------------------------------------------------------------------

def run_search(records, result_rows, target_cluster, cap, checkpoint,
               on_batch=None, batch_size=BATCH_SIZE):
    """构造问题并执行可恢复精确搜索，返回 outcome 字典。"""
    problem = dc.prepare_problem(records, result_rows, target_cluster, cap)
    outcome = dc.solve(problem, checkpoint=checkpoint,
                       on_batch=on_batch, batch_size=batch_size)
    return problem, outcome


def verify_and_publish(problem, outcome, cap):
    """发布闸门：独立逐站复算 + 预算复核，返回 (card_payload, error)。

    计数不一致或超限时返回 (None, 原因)，调用方不得发布判定卡。
    """
    rule = outcome['rule']
    verified = dc.evaluate_rule(problem, rule)
    if (verified['hits'] != outcome['hits']
            or verified['false_accepts'] != outcome['false_accepts']):
        return None, '发布前逐站复算与搜索计数不一致，判定卡不予发布'
    if verified['false_accepts'] > cap:
        return None, '规则误收数超过声明上限，判定卡不予发布'
    rendered = dc.describe_rule(rule)
    payload = {
        'rule': rule,
        'false_accept_cap': cap,
        'hits': verified['hits'],
        'false_accepts': verified['false_accepts'],
        'station_count': problem['n'],
        'target_count': sum(problem['target']),
        'checked_splits': outcome['checked'],
        'total_splits': outcome['total'],
        'judgement_nodes': rendered['judgement_nodes'],
        'max_judgements_per_station': rendered['max_judgements_per_station'],
        'tree': rendered['tree'],
        'steps': rendered['steps'],
        'verified': True}
    return payload, None



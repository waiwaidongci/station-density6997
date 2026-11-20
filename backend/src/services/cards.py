"""复访判定卡：HTTP 编排、并发取代策略与 worker 持久化。

搜索与发布判定的纯函数核心在 card_search.py（无框架依赖，可独立测试），
本模块只负责 SQLAlchemy 仓储、队列与审计。
"""
from fastapi import HTTPException
from sqlalchemy import select
from redis import Redis

from ..config.settings import REDIS_URL
from ..models.card import DecisionCard
from ..models.run import Run
from ..utils.serialization import now, row
from . import decision_card as dc
from .card_search import run_search, verify_and_publish, QUEUE_KEY, BATCH_SIZE
from .audit import log

queue = Redis.from_url(REDIS_URL)


def get_run_or_404(db, run_id):
    r = db.get(Run, run_id)
    if not r:
        raise HTTPException(404, '未找到分析任务')
    return r


def list_cards(db, run_id=None):
    stmt = select(DecisionCard).order_by(DecisionCard.id.desc())
    if run_id is not None:
        stmt = stmt.where(DecisionCard.run_id == run_id)
    return [row(c) for c in db.scalars(stmt)]


def create_card(db, user, data):
    r = get_run_or_404(db, data.run_id)
    if r.state != 'completed':
        raise HTTPException(409, '只可为已完成研究生成判定卡')
    result = r.result or {}
    cluster_labels = {x.get('cluster') for x in result.get('rows', [])
                      if isinstance(x.get('cluster'), int) and x['cluster'] >= 0}
    if data.target_cluster not in cluster_labels:
        raise HTTPException(422, '目标簇 %s 不在本次研究分区中' % data.target_cluster)
    if data.false_accept_cap < 0:
        raise HTTPException(422, '误收上限不能为负')

    # 并发调低误收上限：同 (run, 簇) 的未结束任务一律标记 superseded，
    # 旧任务即使已在计算也不会发布（worker 发布前二次核验）。
    active = db.scalars(select(DecisionCard).where(
        DecisionCard.run_id == r.id,
        DecisionCard.target_cluster == data.target_cluster,
        DecisionCard.state.in_(('queued', 'running')))).all()
    for old in active:
        old.state = 'superseded'
        old.error = '误收上限已调整为 %s，本任务被新任务取代' % data.false_accept_cap
        old.updated_at = now()

    card = DecisionCard(
        run_id=r.id,
        target_cluster=data.target_cluster,
        false_accept_cap=data.false_accept_cap,
        state='queued',
        progress={},
        created_by=user.email,
        created_at=now(),
        updated_at=now())
    db.add(card)
    db.flush()
    log(db, user, 'card.queued', card.id,
        'run=%s cluster=%s cap=%s' % (r.id, data.target_cluster, data.false_accept_cap))
    db.commit()
    try:
        queue.rpush(QUEUE_KEY, 'card:%s' % card.id)
    except Exception:
        card.state = 'failed'
        card.error = '队列暂不可用，请重新创建判定卡任务'
        card.updated_at = now()
        db.commit()
        raise HTTPException(503, card.error)
    return row(card)


def execute_card(card_id):
    """worker 入口：可恢复的精确搜索 + 发布前独立复算（SQLAlchemy 仓储）。"""
    from ..config.database import Session
    with Session() as db:
        card = db.get(DecisionCard, card_id)
        if not card or card.state not in ('queued', 'running'):
            return
        run = db.get(Run, card.run_id)
        if not run or run.state != 'completed':
            card.state = 'failed'
            card.error = '关联研究不存在或未完成'
            card.updated_at = now()
            db.commit()
            return
        card.state = 'running'
        card.updated_at = now()
        db.commit()
        try:
            def on_batch(progress):
                # 先落盘已核验范围与当前最优（崩溃/重启后从该点恢复，
                # 不重复枚举完整规则、不保存全部候选），再检查取代信号。
                card.progress = {
                    'index': progress['checked'],
                    'total': progress['total'],
                    'best_rule': progress['rule'],
                    'hits': progress['hits'],
                    'false_accepts': progress['false_accepts']}
                card.updated_at = now()
                db.commit()
                # 长任务批次边界刷新 worker 心跳，避免健康检查误判。
                try:
                    from ..config.settings import ARTIFACT_DIR
                    (ARTIFACT_DIR / 'heartbeat').write_text(now())
                except OSError:
                    pass
                db.expire_all()
                fresh = db.get(DecisionCard, card_id)
                return fresh is not None and fresh.state == 'running'

            problem, outcome = run_search(
                run.snapshot['records'], run.result['rows'],
                card.target_cluster, card.false_accept_cap,
                card.progress or None, on_batch=on_batch)

            if not outcome['completed']:
                # 干净中断：进度已在 on_batch 落盘，重新入队等待恢复。
                db.expire_all()
                fresh = db.get(DecisionCard, card_id)
                if fresh is not None and fresh.state == 'running':
                    fresh.state = 'queued'
                    fresh.updated_at = now()
                    db.commit()
                    try:
                        queue.rpush(QUEUE_KEY, 'card:%s' % card_id)
                    except Exception:
                        pass
                return

            # 发布闸门 1：并发取代复核，旧解（含被调低上限取代的）永不发布。
            db.expire_all()
            fresh = db.get(DecisionCard, card_id)
            if fresh is None or fresh.state != 'running':
                return
            if card.false_accept_cap != fresh.false_accept_cap:
                fresh.state = 'superseded'
                fresh.updated_at = now()
                db.commit()
                return

            payload, error = verify_and_publish(
                problem, outcome, fresh.false_accept_cap)
            if error:
                fresh.state = 'failed'
                fresh.error = error
                fresh.updated_at = now()
                log(db, 'worker', 'card.verify_failed', card_id, error)
                db.commit()
                return

            payload['target_cluster'] = card.target_cluster
            payload['frozen_checksum'] = run.result.get('checksum')
            fresh.card = payload
            fresh.progress = {'index': outcome['total'],
                              'total': outcome['total']}
            fresh.state = 'completed'
            fresh.updated_at = now()
            log(db, 'worker', 'card.completed', card_id,
                'hits=%s fa=%s' % (payload['hits'], payload['false_accepts']))
            db.commit()
        except dc.SearchFailed as e:
            card.state = 'failed'
            card.error = str(e)[:2000]
            card.updated_at = now()
            log(db, 'worker', 'card.failed', card_id, card.error)
            db.commit()
        except Exception:
            import logging
            logging.exception('decision card search failed')
            card.state = 'failed'
            card.error = '判定卡搜索失败，请查看 worker 日志'
            card.updated_at = now()
            log(db, 'worker', 'card.failed', card_id, card.error)
            db.commit()

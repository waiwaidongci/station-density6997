from sqlalchemy import Column, Integer, String, JSON, ForeignKey, Text
from ..config.database import Base
from ..config.settings import CONFIG


class DecisionCard(Base):
    """复访判定卡搜索任务及其冻结产物。

    - 同一 (run, target_cluster) 只允许一个有效任务：并发调低误收上限时，
      旧任务在入队时即标记 superseded，worker 完成发布前再次核验，旧解永不发布。
    - progress 持久化已核验根分裂下标与当前最优规则，worker 中断后按批恢复。
    - card 仅在逐站独立复算与搜索计数一致后写入；verified 为发布闸门。
    """
    __tablename__ = CONFIG['card_table']
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey(CONFIG['run_table'] + '.id'), nullable=False)
    target_cluster = Column(Integer, nullable=False)
    false_accept_cap = Column(Integer, nullable=False)
    state = Column(String(20), default='queued')  # queued/running/completed/failed/superseded
    progress = Column(JSON, nullable=False, default=dict)
    card = Column(JSON, nullable=True)
    error = Column(Text, default='')
    created_by = Column(String(120), nullable=False)
    created_at = Column(String(40), nullable=False)
    updated_at = Column(String(40), nullable=False)

import json,time,logging
from redis import Redis
from sqlalchemy import select
from .config.database import Session
from .config.settings import REDIS_URL,ARTIFACT_DIR
from .models.run import Run
from .models import source,record,user,note,audit
from .services.algorithm import calculate
from .services.cards import execute_card,QUEUE_KEY
from .services.audit import log
from .services.export import csv_response
queue=Redis.from_url(REDIS_URL)
ARTIFACT_DIR.mkdir(parents=True,exist_ok=True)
def execute(id):
    with Session() as db:
        r=db.get(Run,id)
        if not r or r.state!='queued':return
        r.state='running';db.commit()
        try:
            result=calculate(r.snapshot['records'],r.parameters)
            json.dumps(result,allow_nan=False)
            r.result=result;r.state='completed'
            response=csv_response(result,'run.csv')
            (ARTIFACT_DIR/f'run-{r.id}.csv').write_bytes(response.body)
            log(db,'worker','run.completed',r.id)
        except Exception as e:
            logging.exception('analysis failed');r.state='failed';r.error=str(e)[:2000]
            log(db,'worker','run.failed',r.id,r.error)
        db.commit()
def heartbeat():
    (ARTIFACT_DIR/'heartbeat').write_text(str(time.time()))


def main():
    while True:
        heartbeat()
        # 分析队列优先非空取走；空时阻塞等待判定卡队列，兼顾两类任务的及时性。
        value=queue.lpop('analysis:queue')
        if value is None:
            value=queue.blpop(QUEUE_KEY,timeout=2)
            if value is not None:
                value=value[1]
        if value is None:
            continue
        kind,_,payload=value.partition(b':')
        try:
            if kind==b'card':
                execute_card(int(payload))
            elif kind==b'run':
                execute(int(payload))
            else:
                logging.warning('unknown queue message ignored: %r', value)
        except Exception:
            logging.exception('worker job failed')
        heartbeat()
if __name__=='__main__':main()

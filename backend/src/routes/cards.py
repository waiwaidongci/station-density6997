from fastapi import APIRouter, Depends

from ..config.database import db_session
from ..schemas.card import CardInput
from ..services.auth import current_user, writer
from ..controllers.cards import CardsController as C

router = APIRouter()


@router.get('/cards')
def cards(run_id: int | None = None, db=Depends(db_session),
          u=Depends(current_user)):
    return C.list(db, run_id)


@router.post('/cards')
def create(data: CardInput, db=Depends(db_session), u=Depends(writer)):
    return C.create(db, u, data)

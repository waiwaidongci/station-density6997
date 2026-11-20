from pydantic import BaseModel, Field


class CardInput(BaseModel):
    run_id: int
    target_cluster: int
    false_accept_cap: int = Field(ge=0, le=100000)

from ..services import cards


class CardsController:
    list = staticmethod(cards.list_cards)
    create = staticmethod(cards.create_card)

"""Данные заказа."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Order:
    number: str
    amount: int

    def is_free(self) -> bool:
        return self.amount == 0

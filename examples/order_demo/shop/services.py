"""Операции над заказами."""
from shop.models import Order


class OrderService:
    def create(self, number: str, amount: int) -> Order:
        return Order(number=number, amount=amount)

    def describe(self, order: Order) -> str:
        return f"Order {order.number}: {order.amount}"

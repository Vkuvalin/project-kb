"""Точка входа маленького магазина."""
from shop.services import OrderService


def main() -> str:
    service = OrderService()
    order = service.create("A-001", 1200)
    return service.describe(order)


if __name__ == "__main__":
    print(main())

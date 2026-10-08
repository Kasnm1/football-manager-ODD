from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Callable


RouteHandler = Callable[[Any, dict[str, Any], dict[str, list[str]]], Any]


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    handler: RouteHandler
    success_status: HTTPStatus = HTTPStatus.OK
    name: str = ""


@dataclass(frozen=True)
class RouteResult:
    status: HTTPStatus
    payload: dict[str, Any]


class RouteRegistry:
    """Validated exact-path registry used by the local HTTP adapter."""

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], Route] = {}

    def register(self, route: Route) -> None:
        key = (route.method.upper(), route.path)
        if not route.path.startswith("/"):
            raise ValueError(f"路由必须使用绝对路径：{route.path}")
        if key in self._routes:
            raise ValueError(f"重复路由：{key[0]} {key[1]}")
        self._routes[key] = route

    def add(
        self, method: str, path: str, handler: RouteHandler, *,
        status: HTTPStatus = HTTPStatus.OK, name: str = "",
    ) -> None:
        self.register(Route(method.upper(), path, handler, status, name or handler.__name__))

    def resolve(self, method: str, path: str) -> Route | None:
        return self._routes.get((method.upper(), path))

    def dispatch(
        self, method: str, path: str, context: Any,
        payload: dict[str, Any] | None = None,
        query: dict[str, list[str]] | None = None,
    ) -> RouteResult | None:
        route = self.resolve(method, path)
        if route is None:
            return None
        result = route.handler(context, payload or {}, query or {})
        if not isinstance(result, dict):
            raise TypeError(f"路由 {route.name} 必须返回 JSON 对象")
        return RouteResult(route.success_status, result)

    def describe(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            (method, path, route.name)
            for (method, path), route in sorted(self._routes.items())
        )

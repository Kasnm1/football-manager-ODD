from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP, localcontext
from copy import deepcopy
from typing import Any, MutableMapping


MINOR_SCALE = 100
_MAX_MAJOR_ADJUSTED = 307


class InvalidMoney(ValueError):
    """Raised when an external or persisted value is not finite money."""


def _finite_decimal(value: Any, label: str) -> Decimal:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise InvalidMoney(f"{label}必须是有效数字") from error
    if not decimal.is_finite():
        raise InvalidMoney(f"{label}必须是有限数值")
    if decimal and decimal.adjusted() > _MAX_MAJOR_ADJUSTED:
        raise InvalidMoney(f"{label}超出程序可处理范围")
    return decimal


def _coefficient_digits(value: Decimal) -> int:
    return max(1, len(value.as_tuple().digits))


def to_minor(value: Any) -> int:
    """Convert a major-unit value to integer pennies using decimal rounding."""
    decimal = _finite_decimal(value, "金额")
    try:
        # quantize() uses the thread's default precision (normally 28). Old
        # account data can contain longer finite amounts, so direct quantizing
        # used to abort save connection while migrating those fields.
        with localcontext() as context:
            context.prec = max(28, _coefficient_digits(decimal) + 2)
            return int(
                (decimal * MINOR_SCALE).to_integral_value(
                    rounding=ROUND_HALF_UP,
                )
            )
    except (InvalidOperation, OverflowError, ValueError) as error:
        raise InvalidMoney("金额超出程序可处理范围") from error


def from_minor(value: Any) -> float:
    try:
        minor = int(value)
    except (TypeError, ValueError) as error:
        raise InvalidMoney("金额最小单位必须是整数") from error
    return float(Decimal(minor) / MINOR_SCALE)


def round_money(value: Any) -> float:
    return from_minor(to_minor(value))


def multiply_minor(minor: int, multiplier: Any) -> int:
    """Multiply posted money without passing through binary floating point."""
    factor = _finite_decimal(multiplier, "金额乘数")
    try:
        minor_decimal = Decimal(int(minor))
        with localcontext() as context:
            context.prec = max(
                28,
                _coefficient_digits(minor_decimal)
                + _coefficient_digits(factor)
                + 2,
            )
            product = minor_decimal * factor
            if product and product.adjusted() > _MAX_MAJOR_ADJUSTED + 2:
                raise InvalidMoney("金额计算结果超出程序可处理范围")
            return int(product.to_integral_value(rounding=ROUND_HALF_UP))
    except InvalidMoney:
        raise
    except (InvalidOperation, OverflowError, TypeError, ValueError) as error:
        raise InvalidMoney("金额计算结果超出程序可处理范围") from error


def maximum_minor_for_product(maximum_minor: int, multiplier: Any) -> int:
    """Return the largest non-negative minor-unit input whose rounded product fits."""
    factor = _finite_decimal(multiplier, "金额乘数")
    if factor <= 0:
        raise InvalidMoney("金额乘数必须是大于 0 的有限数值")
    maximum_minor = max(0, int(maximum_minor))
    with localcontext() as context:
        context.prec = max(
            28,
            len(str(maximum_minor)) + _coefficient_digits(factor) + 2,
        )
        candidate = int(
            (Decimal(maximum_minor) / factor).to_integral_value(
                rounding=ROUND_FLOOR,
            )
        )
    while multiply_minor(candidate + 1, factor) <= maximum_minor:
        candidate += 1
    while candidate > 0 and multiply_minor(candidate, factor) > maximum_minor:
        candidate -= 1
    return candidate


def read_minor(
    payload: MutableMapping[str, Any], field: str, *, default: Any = 0,
) -> int:
    """Read canonical minor units, migrating only records that lack them."""
    minor_field = f"{field}_minor"
    if minor_field in payload:
        raw_minor = payload[minor_field]
        if isinstance(raw_minor, bool) or not isinstance(raw_minor, int):
            raise InvalidMoney(f"{minor_field} 必须是整数")
        minor = raw_minor
    else:
        minor = to_minor(payload.get(field, default))
        payload[minor_field] = minor
    payload[field] = from_minor(minor)
    return minor


def write_minor(
    payload: MutableMapping[str, Any], field: str, minor: int,
) -> int:
    minor = int(minor)
    payload[f"{field}_minor"] = minor
    payload[field] = from_minor(minor)
    return minor


def migrate_money_fields(
    payload: MutableMapping[str, Any], fields: tuple[str, ...],
) -> bool:
    changed = False
    for field in fields:
        if field not in payload and f"{field}_minor" not in payload:
            continue
        before_minor = payload.get(f"{field}_minor")
        before_major = payload.get(field)
        minor = read_minor(payload, field)
        changed = changed or before_minor != minor or before_major != from_minor(minor)
    return changed


class _NormalizedMoneyRow(dict):
    """Transient validation state; JSON serialization keeps only business data."""

    __slots__ = ("_fields", "_money_keys", "_dirty")

    def __init__(self, row: dict, fields: tuple[str, ...]) -> None:
        super().__init__(row)
        self._fields = fields
        self._money_keys = frozenset(fields) | frozenset(f"{field}_minor" for field in fields)
        self._dirty = False

    def __setitem__(self, key: Any, value: Any) -> None:
        if key in self._money_keys:
            self._dirty = True
        super().__setitem__(key, value)

    def __delitem__(self, key: Any) -> None:
        super().__delitem__(key)
        if key in self._money_keys:
            self._dirty = True

    def update(self, *args: Any, **kwargs: Any) -> None:
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def setdefault(self, key: Any, default: Any = None) -> Any:
        if key not in self:
            self[key] = default
        return self[key]

    def pop(self, key: Any, *default: Any) -> Any:
        if key in self._money_keys and key in self:
            self._dirty = True
        return super().pop(key, *default)

    def popitem(self) -> tuple[Any, Any]:
        key, value = super().popitem()
        if key in self._money_keys:
            self._dirty = True
        return key, value

    def clear(self) -> None:
        self._dirty = self._dirty or bool(self._money_keys.intersection(self))
        super().clear()

    def __ior__(self, other: Any):
        self.update(other)
        return self

    def __deepcopy__(self, memo: dict):
        # Account document reads and commits copy rows. Preserve validation
        # state while keeping each returned document independently mutable.
        result = type(self).__new__(type(self))
        memo[id(self)] = result
        result._fields = self._fields
        result._money_keys = self._money_keys
        for key, value in self.items():
            dict.__setitem__(result, deepcopy(key, memo), deepcopy(value, memo))
        result._dirty = self._dirty
        return result


def migrate_money_records(records: list | dict, fields: tuple[str, ...]) -> bool:
    """Normalize new/edited records, retaining clean rows across document copies."""
    changed = False
    entries = records.items() if isinstance(records, dict) else enumerate(records)
    for key, row in entries:
        if not isinstance(row, dict):
            continue
        if isinstance(row, _NormalizedMoneyRow) and row._fields == fields:
            if not row._dirty:
                continue
            changed = migrate_money_fields(row, fields) or changed
            row._dirty = False
        else:
            changed = migrate_money_fields(row, fields) or changed
            records[key] = _NormalizedMoneyRow(row, fields)
    return changed

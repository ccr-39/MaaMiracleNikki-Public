"""Shared validation for the selectable makeup fee tiers."""
MAKEUP_TIERS = (0, 50, 100)


def validate_makeup_tiers(value):
    if (not isinstance(value, list)
            or any(type(fee) is not int or fee not in MAKEUP_TIERS for fee in value)
            or len(value) != len(set(value))):
        raise ValueError("补签档位只能选择免费、50钻、100钻")
    return sorted(value)

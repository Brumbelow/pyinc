from __future__ import annotations

from collections.abc import Callable

_input_type: type[object] | None = None
_query_type: type[object] | None = None
_file_stat_resource_type: type[object] | None = None
_file_stat_public_value: Callable[[object], object] | None = None


def register_core_types(input_type: type[object], query_type: type[object]) -> None:
    global _input_type, _query_type
    _input_type = input_type
    _query_type = query_type


def register_file_stat_boundary(
    resource_type: type[object], public_value: Callable[[object], object]
) -> None:
    global _file_stat_resource_type, _file_stat_public_value
    _file_stat_resource_type = resource_type
    _file_stat_public_value = public_value


def is_exact_input(value: object) -> bool:
    input_type = _input_type
    return input_type is not None and type(value) is input_type


def is_exact_query(value: object) -> bool:
    query_type = _query_type
    return query_type is not None and type(value) is query_type


def file_stat_public_value(resource: object, value: object) -> object:
    resource_type = _file_stat_resource_type
    converter = _file_stat_public_value
    if resource_type is not None and converter is not None and isinstance(resource, resource_type):
        return converter(value)
    return value

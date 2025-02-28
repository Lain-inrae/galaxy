"""
Allows merging overlapping openapi definitions.
Taken from https://github.com/tiangolo/fastapi/pull/4727
"""
import http.client
import inspect
import itertools
import warnings
from enum import Enum
from typing import (
    Any,
    Callable,
    cast,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    Union,
)

from fastapi import routing
from fastapi.datastructures import DefaultPlaceholder
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import (
    get_flat_dependant,
    get_flat_params,
)
from fastapi.encoders import jsonable_encoder
from fastapi.openapi.constants import (
    METHODS_WITH_BODY,
    REF_PREFIX,
)
from fastapi.openapi.models import OpenAPI
from fastapi.params import (
    Body,
    Param,
)
from fastapi.responses import Response
from fastapi.utils import (
    deep_dict_update,
    generate_operation_id_for_path,
    get_model_definitions,
    is_body_allowed_for_status_code,
)
from pydantic import BaseModel
from pydantic.fields import (
    ModelField,
    Undefined,
)
from pydantic.schema import (
    field_schema,
    get_flat_models_from_fields,
    get_model_name_map,
)
from pydantic.utils import lenient_issubclass
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute
from starlette.status import HTTP_422_UNPROCESSABLE_ENTITY

validation_error_definition = {
    "title": "ValidationError",
    "type": "object",
    "properties": {
        "loc": {"title": "Location", "type": "array", "items": {"type": "string"}},
        "msg": {"title": "Message", "type": "string"},
        "type": {"title": "Error Type", "type": "string"},
    },
    "required": ["loc", "msg", "type"],
}

validation_error_response_definition = {
    "title": "HTTPValidationError",
    "type": "object",
    "properties": {
        "detail": {
            "title": "Detail",
            "type": "array",
            "items": {"$ref": REF_PREFIX + "ValidationError"},
        }
    },
}

status_code_ranges: Dict[str, str] = {
    "1XX": "Information",
    "2XX": "Success",
    "3XX": "Redirection",
    "4XX": "Client Error",
    "5XX": "Server Error",
    "DEFAULT": "Default Response",
}


def get_openapi_security_definitions(
    flat_dependant: Dependant,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    security_definitions = {}
    operation_security = []
    for security_requirement in flat_dependant.security_requirements:
        security_definition = jsonable_encoder(
            security_requirement.security_scheme.model,
            by_alias=True,
            exclude_none=True,
        )
        security_name = security_requirement.security_scheme.scheme_name
        security_definitions[security_name] = security_definition
        operation_security.append({security_name: security_requirement.scopes})
    return security_definitions, operation_security


def get_openapi_operation_parameters(
    *,
    all_route_params: Sequence[ModelField],
    model_name_map: Dict[Union[Type[BaseModel], Type[Enum]], str],
) -> List[Dict[str, Any]]:
    parameters = []
    for param in all_route_params:
        field_info = param.field_info
        field_info = cast(Param, field_info)
        if not field_info.include_in_schema:
            continue
        parameter = {
            "name": param.alias,
            "in": field_info.in_.value,
            "required": param.required,
            "schema": field_schema(param, model_name_map=model_name_map, ref_prefix=REF_PREFIX)[0],
        }
        if field_info.description:
            parameter["description"] = field_info.description
        if field_info.examples:
            parameter["examples"] = jsonable_encoder(field_info.examples)
        elif field_info.example != Undefined:
            parameter["example"] = jsonable_encoder(field_info.example)
        if field_info.deprecated:
            parameter["deprecated"] = field_info.deprecated
        parameters.append(parameter)
    return parameters


def get_openapi_operation_request_body(
    *,
    body_field: Optional[ModelField],
    model_name_map: Dict[Union[Type[BaseModel], Type[Enum]], str],
) -> Optional[Dict[str, Any]]:
    if not body_field:
        return None
    assert isinstance(body_field, ModelField)
    body_schema, _, _ = field_schema(body_field, model_name_map=model_name_map, ref_prefix=REF_PREFIX)
    field_info = cast(Body, body_field.field_info)
    request_media_type = field_info.media_type
    required = body_field.required
    request_body_oai: Dict[str, Any] = {}
    if required:
        request_body_oai["required"] = required
    request_media_content: Dict[str, Any] = {"schema": body_schema}
    if field_info.examples:
        request_media_content["examples"] = jsonable_encoder(field_info.examples)
    elif field_info.example != Undefined:
        request_media_content["example"] = jsonable_encoder(field_info.example)
    request_body_oai["content"] = {request_media_type: request_media_content}
    return request_body_oai


def generate_operation_id(*, route: routing.APIRoute, method: str) -> str:  # pragma: nocover
    warnings.warn(
        "fastapi.openapi.utils.generate_operation_id() was deprecated, "
        "it is not used internally, and will be removed soon",
        DeprecationWarning,
        stacklevel=2,
    )
    if route.operation_id:
        return route.operation_id
    path: str = route.path_format
    return generate_operation_id_for_path(name=route.name, path=path, method=method)


def generate_operation_summary(*, route: routing.APIRoute, method: str) -> str:
    if route.summary:
        return route.summary
    return route.name.replace("_", " ").title()


def get_openapi_operation_metadata(*, route: routing.APIRoute, method: str, operation_ids: Set[str]) -> Dict[str, Any]:
    operation: Dict[str, Any] = {}
    if route.tags:
        operation["tags"] = list(set(route.tags))
    operation["summary"] = generate_operation_summary(route=route, method=method)
    if route.description:
        operation["description"] = route.description
    operation_id = route.operation_id or route.unique_id
    if operation_id in operation_ids:
        message = f"Duplicate Operation ID {operation_id} for function " + f"{route.endpoint.__name__}"
        file_name = getattr(route.endpoint, "__globals__", {}).get("__file__")
        if file_name:
            message += f" at {file_name}"
        warnings.warn(message)
    operation_ids.add(operation_id)
    operation["operationId"] = operation_id
    if route.deprecated:
        operation["deprecated"] = route.deprecated
    return operation


def get_openapi_path(
    *, route: routing.APIRoute, model_name_map: Dict[type, str], operation_ids: Set[str]
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    path = {}
    security_schemes: Dict[str, Any] = {}
    definitions: Dict[str, Any] = {}
    assert route.methods is not None, "Methods must be a list"
    if isinstance(route.response_class, DefaultPlaceholder):
        current_response_class: Type[Response] = route.response_class.value
    else:
        current_response_class = route.response_class
    assert current_response_class, "A response class is needed to generate OpenAPI"
    route_response_media_type: Optional[str] = current_response_class.media_type
    if route.include_in_schema:
        for method in route.methods:
            operation = get_openapi_operation_metadata(route=route, method=method, operation_ids=operation_ids)
            parameters: List[Dict[str, Any]] = []
            flat_dependant = get_flat_dependant(route.dependant, skip_repeats=True)
            security_definitions, operation_security = get_openapi_security_definitions(flat_dependant=flat_dependant)
            # https://redocly.com/docs/cli/rules/security-defined/#api-design-principles
            # be explicit that this is an unsecured endpoint, no API token is needed
            # (unless required by proxy)
            operation.setdefault("security", []).extend(operation_security)
            if security_definitions:
                security_schemes.update(security_definitions)
            all_route_params = get_flat_params(route.dependant)
            operation_parameters = get_openapi_operation_parameters(
                all_route_params=all_route_params, model_name_map=model_name_map
            )
            parameters.extend(operation_parameters)
            if parameters:
                operation["parameters"] = list({param["name"]: param for param in parameters}.values())
            if method in METHODS_WITH_BODY:
                request_body_oai = get_openapi_operation_request_body(
                    body_field=route.body_field, model_name_map=model_name_map
                )
                if request_body_oai:
                    operation["requestBody"] = request_body_oai
            if route.callbacks:
                callbacks = {}
                for callback in route.callbacks:
                    if isinstance(callback, routing.APIRoute):
                        (cb_path, cb_security_schemes, cb_definitions,) = get_openapi_path(
                            route=callback,
                            model_name_map=model_name_map,
                            operation_ids=operation_ids,
                        )
                        callbacks[callback.name] = {callback.path: cb_path}
                operation["callbacks"] = callbacks
            if route.status_code is not None:
                status_code = str(route.status_code)
            else:
                # It would probably make more sense for all response classes to have an
                # explicit default status_code, and to extract it from them, instead of
                # doing this inspection tricks, that would probably be in the future
                # TODO: probably make status_code a default class attribute for all
                # responses in Starlette
                response_signature = inspect.signature(current_response_class.__init__)
                status_code_param = response_signature.parameters.get("status_code")
                if status_code_param is not None:
                    if isinstance(status_code_param.default, int):
                        status_code = str(status_code_param.default)
            operation.setdefault("responses", {}).setdefault(status_code, {})[
                "description"
            ] = route.response_description
            if route_response_media_type and is_body_allowed_for_status_code(route.status_code):
                response_schema = {"type": "string"}
                if lenient_issubclass(current_response_class, JSONResponse):
                    if route.response_field:
                        response_schema, _, _ = field_schema(
                            route.response_field,
                            model_name_map=model_name_map,
                            ref_prefix=REF_PREFIX,
                        )
                    else:
                        response_schema = {}
                operation.setdefault("responses", {}).setdefault(status_code, {}).setdefault("content", {}).setdefault(
                    route_response_media_type, {}
                )["schema"] = response_schema
            if route.responses:
                operation_responses = operation.setdefault("responses", {})
                for (
                    additional_status_code,
                    additional_response,
                ) in route.responses.items():
                    process_response = additional_response.copy()
                    process_response.pop("model", None)
                    status_code_key = str(additional_status_code).upper()
                    if status_code_key == "DEFAULT":
                        status_code_key = "default"
                    openapi_response = operation_responses.setdefault(status_code_key, {})
                    assert isinstance(process_response, dict), "An additional response must be a dict"
                    field = route.response_fields.get(additional_status_code)
                    additional_field_schema: Optional[Dict[str, Any]] = None
                    if field:
                        additional_field_schema, _, _ = field_schema(
                            field, model_name_map=model_name_map, ref_prefix=REF_PREFIX
                        )
                        media_type = route_response_media_type or "application/json"
                        additional_schema = (
                            process_response.setdefault("content", {})
                            .setdefault(media_type, {})
                            .setdefault("schema", {})
                        )
                        deep_dict_update(additional_schema, additional_field_schema)
                    status_text: Optional[str] = status_code_ranges.get(
                        str(additional_status_code).upper()
                    ) or http.client.responses.get(int(additional_status_code))
                    description = (
                        process_response.get("description")
                        or openapi_response.get("description")
                        or status_text
                        or "Additional Response"
                    )
                    deep_dict_update(openapi_response, process_response)
                    openapi_response["description"] = description
            http422 = str(HTTP_422_UNPROCESSABLE_ENTITY)
            if (all_route_params or route.body_field) and not any(
                [status in operation["responses"] for status in [http422, "4XX", "default"]]
            ):
                operation["responses"][http422] = {
                    "description": "Validation Error",
                    "content": {"application/json": {"schema": {"$ref": REF_PREFIX + "HTTPValidationError"}}},
                }
                if "ValidationError" not in definitions:
                    definitions.update(
                        {
                            "ValidationError": validation_error_definition,
                            "HTTPValidationError": validation_error_response_definition,
                        }
                    )
            if route.openapi_extra:
                deep_dict_update(operation, route.openapi_extra)
            path[method.lower()] = operation
    return path, security_schemes, definitions


def get_flat_models_from_routes(
    routes: Sequence[BaseRoute],
) -> Set[Union[Type[BaseModel], Type[Enum]]]:
    body_fields_from_routes: List[ModelField] = []
    responses_from_routes: List[ModelField] = []
    request_fields_from_routes: List[ModelField] = []
    callback_flat_models: Set[Union[Type[BaseModel], Type[Enum]]] = set()
    for route in routes:
        if getattr(route, "include_in_schema", None) and isinstance(route, routing.APIRoute):
            if route.body_field:
                assert isinstance(route.body_field, ModelField), "A request body must be a Pydantic Field"
                body_fields_from_routes.append(route.body_field)
            if route.response_field:
                responses_from_routes.append(route.response_field)
            if route.response_fields:
                responses_from_routes.extend(route.response_fields.values())
            if route.callbacks:
                callback_flat_models |= get_flat_models_from_routes(route.callbacks)
            params = get_flat_params(route.dependant)
            request_fields_from_routes.extend(params)

    flat_models = callback_flat_models | get_flat_models_from_fields(
        body_fields_from_routes + responses_from_routes + request_fields_from_routes,
        known_models=set(),
    )
    return flat_models


def get_openapi(
    *,
    title: str,
    version: str,
    openapi_version: str = "3.0.3",
    description: Optional[str] = None,
    routes: Sequence[BaseRoute],
    tags: Optional[List[Dict[str, Any]]] = None,
    servers: Optional[List[Dict[str, Union[str, Any]]]] = None,
    terms_of_service: Optional[str] = None,
    contact: Optional[Dict[str, Union[str, Any]]] = None,
    license_info: Optional[Dict[str, Union[str, Any]]] = None,
) -> Dict[str, Any]:
    info: Dict[str, Any] = {"title": title, "version": version}
    if description:
        info["description"] = description
    if terms_of_service:
        info["termsOfService"] = terms_of_service
    if contact:
        info["contact"] = contact
    if license_info:
        info["license"] = license_info
    output: Dict[str, Any] = {"openapi": openapi_version, "info": info}
    if servers:
        output["servers"] = servers
    components: Dict[str, Dict[str, Any]] = {}
    paths: Dict[str, Dict[str, Any]] = {}
    operation_ids: Set[str] = set()
    flat_models = get_flat_models_from_routes(routes)
    model_name_map = get_model_name_map(flat_models)
    definitions = get_model_definitions(flat_models=flat_models, model_name_map=model_name_map)
    for route in routes:
        if isinstance(route, routing.APIRoute):
            result = get_openapi_path(route=route, model_name_map=model_name_map, operation_ids=operation_ids)
            if result:
                path, security_schemes, path_definitions = result
                if path:
                    existing_path = paths.get(route.path_format)
                    paths[route.path_format] = merge_paths(existing_path, path) if existing_path else path
                if security_schemes:
                    components.setdefault("securitySchemes", {}).update(security_schemes)
                if path_definitions:
                    definitions.update(path_definitions)
    if definitions:
        components["schemas"] = {k: definitions[k] for k in sorted(definitions)}
    if components:
        output["components"] = components
    output["paths"] = paths
    if tags:
        output["tags"] = tags
    return jsonable_encoder(OpenAPI(**output), by_alias=True, exclude_none=True)  # type: ignore


def merge_paths(existing_path: Dict[str, Any], new_path: Dict[str, Any]) -> Dict[str, Any]:
    """Merge two openapi path descriptions for the same route, e.g. for response content with different accept-encoding."""
    path_by_operation_id: Dict[str, Dict[str, Any]] = {}
    for method, operation in itertools.chain(existing_path.items(), new_path.items()):
        path_by_operation_id.setdefault(operation["operationId"], {})[method] = operation
    return deep_dict_operation_merge(path_by_operation_id) or {}


def merge_tags(tags_by_id: Dict[str, Optional[List[str]]]) -> Optional[List[str]]:
    tags: Optional[List[str]] = None
    for update_tags in tags_by_id.values():
        if update_tags:
            tags = tags or []
            tags.extend(t for t in update_tags if t not in tags)
    return tags


def merge_summary(summary_by_id: Dict[str, Optional[str]]) -> Optional[str]:
    summary: Optional[str] = None
    for update_summary in summary_by_id.values():
        if update_summary and summary != update_summary:
            summary = f"{summary} / {update_summary}" if summary else update_summary
    return summary


def merge_description(desc_by_id: Dict[str, Optional[str]]) -> Optional[str]:
    desc: Optional[str] = None
    for update_desc in desc_by_id.values():
        if update_desc and desc != update_desc:
            desc = f"{desc}\n\n OR \n\n {update_desc}" if desc else update_desc
    return desc


def merge_operation_id(operation_id_by_id: Dict[str, Optional[str]]) -> Optional[str]:
    operation_id: Optional[str] = None
    for update_operation_id in operation_id_by_id.values():
        if update_operation_id and operation_id != update_operation_id:
            if operation_id:
                message = f"Merging operation with id {operation_id} with operation with id {update_operation_id}."
                warnings.warn(message)
                operation_id = f"{operation_id}+{update_operation_id}"
            else:
                operation_id = update_operation_id
    return operation_id


def merge_deprecated(deprecated_by_id: Dict[str, Optional[bool]]) -> Optional[bool]:
    if any(deprecated_by_id.values()):
        return True
    return None


def merge_parameters(params_by_id: Dict[str, Optional[List[Dict[str, Any]]]]) -> Optional[List[Dict[str, Any]]]:
    with_params = [(param_id, params) for (param_id, params) in params_by_id.items() if params]
    if not with_params:
        return None
    if len(with_params) == 1:
        return with_params[0][1]
    param_by_id_by_name: Dict[str, Any] = {}
    for (param_id, params) in with_params:
        for param in params:
            param_by_id_by_name.setdefault(param["name"], {})[param_id] = param

    return [deep_dict_operation_merge(param_by_id) for param_by_id in param_by_id_by_name.values()]


def deep_dict_operation_merge(dict_by_id: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    if not dict_by_id:
        return dict_by_id
    if len(dict_by_id) == 1:
        return next(iter(dict_by_id.values()))
    keys = {k for d in dict_by_id.values() for k in d}
    result_dict: Optional[Dict[Any, Any]] = None
    for key in keys:
        merge_method = MERGE_METHODS_BY_KEY.get(key)
        if not merge_method and _all_dict_or_none_at_key(dict_by_id, key):
            merge_method = deep_dict_operation_merge
        result_dict = result_dict or {}
        dict_at_key_by_id = {d_id: d[key] for d_id, d in dict_by_id.items() if key in d}
        if merge_method:
            result_dict[key] = merge_method(dict_at_key_by_id)
        else:
            # use last entry at key for each
            result_dict[key] = next(reversed(list(dict_at_key_by_id.values())), None)
    return {k: v for k, v in result_dict.items() if v is not None} if result_dict else {}


def _all_dict_or_none_at_key(dict_by_id: Dict[str, Dict[str, Any]], key: str) -> bool:
    return all(isinstance(d[key], dict) for d in dict_by_id.values() if key in d if d[key] is not None)


MERGE_METHODS_BY_KEY: Dict[str, Callable[[Dict[str, Any]], Any]] = {
    "tags": merge_tags,
    "summary": merge_summary,
    "description": merge_description,
    "operationId": merge_operation_id,
    "parameters": merge_parameters,
    "deprecated": merge_deprecated,
    "content": deep_dict_operation_merge,
    "requestBody": deep_dict_operation_merge,
    "callbacks": deep_dict_operation_merge,
}

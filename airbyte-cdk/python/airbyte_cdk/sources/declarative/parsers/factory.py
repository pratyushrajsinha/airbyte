#
# Copyright (c) 2022 Airbyte, Inc., all rights reserved.
#

from __future__ import annotations

import copy
import importlib
from typing import Any, Mapping, Optional, Type, Union, get_args, get_origin, get_type_hints

from airbyte_cdk.sources.declarative.create_partial import create
from airbyte_cdk.sources.declarative.interpolation.jinja import JinjaInterpolation
from airbyte_cdk.sources.declarative.parsers.class_types_registry import CLASS_TYPES_REGISTRY
from airbyte_cdk.sources.declarative.parsers.default_implementation_registry import DEFAULT_IMPLEMENTATIONS_REGISTRY
from airbyte_cdk.sources.declarative.types import Config
from jsonschema import validate


class DeclarativeComponentFactory:
    def __init__(self):
        self._interpolator = JinjaInterpolation()

    def resolve(self, component_definition: Mapping[str, Any]):
        definition = copy.deepcopy(component_definition)
        class_name = self.get_class_name(component_definition)
        definition["class_name"] = class_name
        return definition

    def create_component(self, component_definition: Mapping[str, Any], config: Config, instantiate):
        """

        :param component_definition: mapping defining the object to create. It should have at least one field: `class_name`
        :param config: Connector's config
        :return: the object to create
        """
        kwargs = copy.deepcopy(component_definition)
        class_name = kwargs.pop("class_name")
        return self.build(class_name, config, instantiate, **kwargs)

    def build(self, class_or_class_name: Union[str, Type], config, instantiate, **kwargs):
        if isinstance(class_or_class_name, str):
            class_ = self._get_class_from_fully_qualified_class_name(class_or_class_name)
        else:
            class_ = class_or_class_name

        # create components in options before propagating them
        if "options" in kwargs:
            kwargs["options"] = {
                k: self._create_subcomponent(k, v, kwargs, config, class_, instantiate) for k, v in kwargs["options"].items()
            }

        updated_kwargs = {k: self._create_subcomponent(k, v, kwargs, config, class_, instantiate) for k, v in kwargs.items()}
        if instantiate:
            return create(class_, config=config, **updated_kwargs)
        else:
            schema = class_.json_schema()
            print(f"validating for {class_or_class_name}")
            # full_definition = {**{"config": config}, **updated_kwargs}
            full_definition = {**updated_kwargs, **{k: v for k, v in updated_kwargs["options"].items() if k not in updated_kwargs}}
            full_definition["config"] = {}
            validate(full_definition, schema)
            return lambda: full_definition

    @staticmethod
    def _get_class_from_fully_qualified_class_name(class_name: str):
        split = class_name.split(".")
        module = ".".join(split[:-1])
        class_name = split[-1]
        return getattr(importlib.import_module(module), class_name)

    @staticmethod
    def _merge_dicts(d1, d2):
        return {**d1, **d2}

    def get_class_name(self, definition) -> Optional[str]:
        if self.is_object_definition_with_class_name(definition):
            return definition["class_name"]
        elif self.is_object_definition_with_type(definition):
            object_type = definition.pop("type")
            return CLASS_TYPES_REGISTRY[object_type]
        elif isinstance(definition, dict):
            return dict
        else:
            return dict

    def _create_subcomponent(self, key, definition, kwargs, config, parent_class, instantiate):
        """
        There are 5 ways to define a component.
        1. dict with "class_name" field -> create an object of type "class_name"
        2. dict with "type" field -> lookup the `CLASS_TYPES_REGISTRY` to find the type of object and create an object of that type
        3. a dict with a type that can be inferred. If the parent class's constructor has type hints, we can infer the type of the object to create by looking up the `DEFAULT_IMPLEMENTATIONS_REGISTRY` map
        4. list: loop over the list and create objects for its items
        5. anything else -> return as is
        """
        if self.is_object_definition_with_class_name(definition):
            # propagate kwargs to inner objects
            definition["options"] = self._merge_dicts(kwargs.get("options", dict()), definition.get("options", dict()))
            return self.create_component(definition, config, instantiate)()
        elif self.is_object_definition_with_type(definition):
            # If type is set instead of class_name, get the class_name from the CLASS_TYPES_REGISTRY
            definition["options"] = self._merge_dicts(kwargs.get("options", dict()), definition.get("options", dict()))
            object_type = definition.pop("type")
            class_name = CLASS_TYPES_REGISTRY[object_type]
            definition["class_name"] = class_name
            return self.create_component(definition, config, instantiate)()
        elif isinstance(definition, dict):
            # Try to infer object type
            expected_type = self.get_default_type(key, parent_class)
            if expected_type:
                definition["class_name"] = expected_type
                definition["options"] = {
                    k: v for k, v in self._merge_dicts(kwargs.get("options", dict()), definition.get("options", dict())).items() if k != key
                }
                if key == "retriever":
                    print("sdfg")
                    print(definition["options"])
                    # exit()
                return self.create_component(definition, config, instantiate)()
            else:
                return definition
        elif isinstance(definition, list):
            return [
                self._create_subcomponent(
                    key,
                    sub,
                    self._merge_dicts(kwargs.get("options", dict()), self._get_subcomponent_options(sub)),
                    config,
                    parent_class,
                    instantiate,
                )
                for sub in definition
            ]
        else:
            return definition

    @staticmethod
    def is_object_definition_with_class_name(definition):
        return isinstance(definition, dict) and "class_name" in definition

    @staticmethod
    def is_object_definition_with_type(definition):
        return isinstance(definition, dict) and "type" in definition

    @staticmethod
    def get_default_type(parameter_name, parent_class):
        type_hints = get_type_hints(parent_class.__init__)
        interface = type_hints.get(parameter_name)
        origin = get_origin(interface)
        if origin:
            # Handling Optional, which are implement as a Union[T, None]
            # the interface we're looking for being the first type argument
            args = get_args(interface)
            interface = args[0]

        expected_type = DEFAULT_IMPLEMENTATIONS_REGISTRY.get(interface)
        return expected_type

    @staticmethod
    def _get_subcomponent_options(sub: Any):
        if isinstance(sub, dict):
            return sub.get("options", {})
        else:
            return {}

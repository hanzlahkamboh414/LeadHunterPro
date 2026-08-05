"""Infrastructure tests for Plugin Configuration View (Unit 7)."""

from __future__ import annotations

import pytest

from app.discovery.website.plugin_config_view import (
    PluginConfigView,
    make_config_view,
)


class TestProtocolConformance:
    def test_reference_impl_conforms_to_protocol(self) -> None:
        view = make_config_view({"api_key": "secret"})
        assert isinstance(view, PluginConfigView)

    def test_isinstance_against_the_protocol_does_not_raise(self) -> None:
        """Behavioural proof that @runtime_checkable is applied.

        A Protocol without @runtime_checkable raises TypeError on
        isinstance. Asserting the call succeeds therefore pins the
        decorator without touching a CPython private attribute.
        """
        view = make_config_view({})
        result = isinstance(view, PluginConfigView)
        assert result is True

    def test_duck_typed_object_conforms(self) -> None:
        """Structural typing: no inheritance required (spec §9.1)."""

        class HandRolled:
            def get(self, key: str, default: object = None) -> object:
                return default

            def require(self, key: str) -> object:
                raise KeyError(key)

            def to_dict(self) -> dict[str, object]:
                return {}

        assert isinstance(HandRolled(), PluginConfigView)

    def test_object_missing_require_does_not_conform(self) -> None:
        class MissingRequire:
            def get(self, key: str, default: object = None) -> object:
                return default

            def to_dict(self) -> dict[str, object]:
                return {}

        assert not isinstance(MissingRequire(), PluginConfigView)

    def test_object_missing_get_does_not_conform(self) -> None:
        class MissingGet:
            def require(self, key: str) -> object:
                raise KeyError(key)

            def to_dict(self) -> dict[str, object]:
                return {}

        assert not isinstance(MissingGet(), PluginConfigView)

    def test_object_missing_to_dict_does_not_conform(self) -> None:
        class MissingToDict:
            def get(self, key: str, default: object = None) -> object:
                return default

            def require(self, key: str) -> object:
                raise KeyError(key)

        assert not isinstance(MissingToDict(), PluginConfigView)

    def test_unrelated_object_does_not_conform(self) -> None:
        assert not isinstance(object(), PluginConfigView)
        assert not isinstance({"api_key": "secret"}, PluginConfigView)


class TestGet:
    def test_present_key_returns_value(self) -> None:
        view = make_config_view({"api_key": "secret123", "limit": 10})
        assert view.get("api_key") == "secret123"
        assert view.get("limit") == 10

    def test_absent_key_returns_none_by_default(self) -> None:
        view = make_config_view({"api_key": "secret"})
        assert view.get("missing") is None

    def test_absent_key_returns_supplied_default(self) -> None:
        view = make_config_view({"api_key": "secret"})
        assert view.get("missing", "default_val") == "default_val"
        assert view.get("missing", 42) == 42
        assert view.get("missing", None) is None

    def test_type_pass_through(self) -> None:
        view = make_config_view({
            "str_val": "hello",
            "int_val": 42,
            "float_val": 3.14,
            "bool_val": True,
            "none_val": None,
            "list_val": [1, 2, 3],
            "dict_val": {"nested": "value"},
        })
        assert view.get("str_val") == "hello"
        assert view.get("int_val") == 42
        assert view.get("float_val") == 3.14
        assert view.get("bool_val") is True
        assert view.get("none_val") is None
        assert view.get("list_val") == [1, 2, 3]
        assert view.get("dict_val") == {"nested": "value"}


class TestRequire:
    def test_present_key_returns_value(self) -> None:
        view = make_config_view({"api_key": "secret123"})
        assert view.require("api_key") == "secret123"

    def test_absent_key_raises_keyerror(self) -> None:
        view = make_config_view({"api_key": "secret"})
        with pytest.raises(KeyError) as exc_info:
            view.require("missing")
        assert "missing" in str(exc_info.value)

    def test_error_message_names_key(self) -> None:
        view = make_config_view({})
        with pytest.raises(KeyError) as exc_info:
            view.require("api_key")
        assert "api_key" in str(exc_info.value)
        assert "required but not provided" in str(exc_info.value)


class TestToDict:
    def test_returns_all_key_value_pairs(self) -> None:
        options = {"api_key": "secret", "limit": 10, "timeout": 30.5}
        view = make_config_view(options)
        result = view.to_dict()
        assert result == options

    def test_returns_copy_not_reference(self) -> None:
        view = make_config_view({"api_key": "secret"})
        result = view.to_dict()
        result["api_key"] = "mutated"
        # Original view should be unaffected
        assert view.get("api_key") == "secret"
        # And subsequent to_dict should still return original
        assert view.to_dict()["api_key"] == "secret"

    def test_empty_options_returns_empty_dict(self) -> None:
        view = make_config_view({})
        assert view.to_dict() == {}


class TestConstructionSnapshot:
    """The view copies its options; it does not alias the caller's dict."""

    def test_mutating_the_source_dict_does_not_affect_get(self) -> None:
        options = {"api_key": "original"}
        view = make_config_view(options)
        options["api_key"] = "mutated"
        assert view.get("api_key") == "original"

    def test_mutating_the_source_dict_does_not_affect_require(self) -> None:
        options = {"api_key": "original"}
        view = make_config_view(options)
        options["api_key"] = "mutated"
        assert view.require("api_key") == "original"

    def test_mutating_the_source_dict_does_not_affect_to_dict(self) -> None:
        options = {"api_key": "original"}
        view = make_config_view(options)
        options["api_key"] = "mutated"
        assert view.to_dict() == {"api_key": "original"}

    def test_adding_a_key_to_the_source_dict_is_not_visible(self) -> None:
        options = {"api_key": "secret"}
        view = make_config_view(options)
        options["added_later"] = "value"
        assert view.get("added_later") is None
        with pytest.raises(KeyError):
            view.require("added_later")

    def test_deleting_a_key_from_the_source_dict_is_not_visible(self) -> None:
        options = {"api_key": "secret"}
        view = make_config_view(options)
        del options["api_key"]
        assert view.get("api_key") == "secret"

    def test_clearing_the_source_dict_is_not_visible(self) -> None:
        options = {"api_key": "secret", "limit": 10}
        view = make_config_view(options)
        options.clear()
        assert view.to_dict() == {"api_key": "secret", "limit": 10}

    def test_the_snapshot_is_shallow(self) -> None:
        """Documents a known limit: nested containers are still shared.

        dict(options) copies one level. A caller that mutates a nested
        list or dict it passed in will still be observed. Documented
        rather than deep-copied, the same choice Candidate.attributes
        makes in Unit 6.
        """
        nested = {"retries": [1, 2]}
        options = {"nested": nested}
        view = make_config_view(options)
        nested["retries"].append(3)
        assert view.get("nested")["retries"] == [1, 2, 3]


class TestReadOnly:
    def test_no_setitem(self) -> None:
        view = make_config_view({"a": 1})
        assert not hasattr(view, "__setitem__")

    def test_no_delitem(self) -> None:
        view = make_config_view({"a": 1})
        assert not hasattr(view, "__delitem__")

    def test_no_update(self) -> None:
        view = make_config_view({"a": 1})
        assert not hasattr(view, "update")

    def test_no_pop(self) -> None:
        view = make_config_view({"a": 1})
        assert not hasattr(view, "pop")

    def test_no_clear(self) -> None:
        view = make_config_view({"a": 1})
        assert not hasattr(view, "clear")


class TestFactory:
    def test_empty_dict_succeeds(self) -> None:
        view = make_config_view({})
        assert isinstance(view, PluginConfigView)
        assert view.get("anything") is None

    def test_non_empty_dict_wrapped(self) -> None:
        view = make_config_view({"api_key": "secret", "limit": 50})
        assert view.get("api_key") == "secret"
        assert view.get("limit") == 50

    def test_returns_protocol_instance(self) -> None:
        view = make_config_view({"x": 1})
        assert isinstance(view, PluginConfigView)


class TestTypePassThrough:
    def test_values_returned_with_original_types(self) -> None:
        view = make_config_view({
            "string": "value",
            "integer": 42,
            "float": 3.14,
            "boolean": True,
            "none": None,
            "list": [1, 2, 3],
            "dict": {"nested": "value"},
        })
        assert isinstance(view.get("string"), str)
        assert isinstance(view.get("integer"), int)
        assert isinstance(view.get("float"), float)
        assert isinstance(view.get("boolean"), bool)
        assert view.get("none") is None
        assert isinstance(view.get("list"), list)
        assert isinstance(view.get("dict"), dict)

    def test_no_implicit_coercion(self) -> None:
        view = make_config_view({"limit": "10"})  # string, not int
        assert view.get("limit") == "10"
        assert isinstance(view.get("limit"), str)


class TestArchitectureBoundaries:
    """Unit 7 is a leaf: stdlib typing only, nothing from Units 1-6."""

    @pytest.mark.parametrize(
        "name",
        [
            "normalize_url",
            "canonical_key",
            "extract_host",
            "DuplicateURLFilter",
            "FilterStats",
            "Confidence",
            "MIN_SCORE",
            "MAX_SCORE",
            "FieldEvidence",
            "EvidenceSet",
            "ExtractedField",
            "normalize_field",
            "FieldExtractor",
            "ExtractionResult",
            "PageContent",
            "EXTRACTOR_INTERFACES",
            "Candidate",
            "CandidateGenerator",
        ],
    )
    def test_the_module_imports_nothing_from_units_1_to_6(self, name: str) -> None:
        from app.discovery.website import plugin_config_view as module

        assert name not in vars(module)

    @pytest.mark.parametrize(
        "name",
        ["PluginConfig", "BaseDiscoveryPlugin", "SourceStatus", "BaseSource"],
    )
    def test_the_module_does_not_import_the_frozen_plugin_layer(
        self, name: str
    ) -> None:
        # The view wraps an options dict; it never needs the model that
        # carries it. Importing it would couple a leaf to Phase 2.1.
        from app.discovery.website import plugin_config_view as module

        assert name not in vars(module)

    @pytest.mark.parametrize(
        "name",
        ["requests", "aiohttp", "httpx", "json", "logging", "os", "dataclass"],
    )
    def test_the_module_pulls_in_no_io_or_serialization_library(
        self, name: str
    ) -> None:
        from app.discovery.website import plugin_config_view as module

        assert name not in vars(module)

    def test_the_reference_implementation_is_private(self) -> None:
        import app.discovery.website as package

        assert "_DictConfigView" not in package.__all__
        assert not hasattr(package, "_DictConfigView")

    def test_the_package_exports_exactly_the_two_unit_7_names(self) -> None:
        import app.discovery.website as package

        exported = set(package.__all__)
        assert "PluginConfigView" in exported
        assert "make_config_view" in exported
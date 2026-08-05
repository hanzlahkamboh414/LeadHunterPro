"""Infrastructure tests for the Candidate Generator Interface (Unit 6).

Phase 2.3A ships an interface only. These tests verify that the contract
is declared, abstract, and that :class:`Candidate` cannot exist in an
invalid state — never that any candidate is correctly discovered, because
no generator is implemented yet.

URL rules are Unit 1's, not Unit 6's. The tests below assert *delegation*
to :func:`normalize_url`; they do not re-test Unit 1's canonicalization
table, which ``test_url_normalizer.py`` already covers.
"""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError

import pytest

from app.discovery.website.candidates import Candidate, CandidateGenerator
from app.discovery.website.confidence import Confidence
from app.discovery.website.url_normalizer import (
    canonical_key,
    extract_host,
    normalize_url,
)

URL = "https://example.com"
GEN = "my_gen"


def make_generator(class_name: str = "MyGen", name: str = ""):
    """Build a concrete generator, since the ABC cannot be instantiated."""
    namespace = {
        "name": name,
        "generate": lambda self, *, industry, location, limit: [],
    }
    return type(class_name, (CandidateGenerator,), namespace)()


class TestCandidateConstruction:
    def test_positional_args(self) -> None:
        c = Candidate(URL, GEN)
        assert c.url == "https://example.com/"
        assert c.generator == GEN

    def test_keyword_args(self) -> None:
        c = Candidate(url=URL, generator=GEN)
        assert c.url == "https://example.com/"
        assert c.generator == GEN

    def test_url_is_required(self) -> None:
        with pytest.raises(TypeError):
            Candidate(generator=GEN)

    def test_generator_is_required(self) -> None:
        with pytest.raises(TypeError):
            Candidate(url=URL)

    def test_confidence_defaults_to_unknown(self) -> None:
        assert Candidate(URL, GEN).confidence is Confidence.UNKNOWN

    def test_attributes_default_to_empty(self) -> None:
        assert Candidate(URL, GEN).attributes == {}

    def test_two_candidates_do_not_share_a_default_attributes_dict(self) -> None:
        first = Candidate(URL, GEN)
        second = Candidate("https://other.com", GEN)
        assert first.attributes is not second.attributes


class TestURLCanonicalization:
    """Unit 6 delegates; it defines no URL rules of its own."""

    def test_bare_domain_gains_https(self) -> None:
        assert Candidate("example.com", GEN).url == "https://example.com/"

    def test_scheme_preserved_never_upgraded(self) -> None:
        assert Candidate("http://example.com", GEN).url == "http://example.com/"

    def test_fragment_dropped(self) -> None:
        assert Candidate("https://example.com#team", GEN).url == "https://example.com/"

    def test_default_port_removed(self) -> None:
        assert Candidate("https://example.com:443", GEN).url == "https://example.com/"

    def test_www_handled_per_unit_1(self) -> None:
        with_www = Candidate("https://www.example.com", GEN)
        without = Candidate("https://example.com", GEN)
        assert with_www.key == without.key


class TestURLValidation:
    def test_non_str_raises_type_error(self) -> None:
        # Without this, 42 would silently become "https://42/".
        with pytest.raises(TypeError, match="Candidate.url must be a str"):
            Candidate(42, GEN)  # type: ignore[arg-type]

    @pytest.mark.parametrize("url", ["", "   "])
    def test_empty_raises_value_error(self, url: str) -> None:
        with pytest.raises(ValueError, match="Candidate.url is required"):
            Candidate(url, GEN)

    @pytest.mark.parametrize(
        "url",
        [
            "mailto:sales@example.com",
            "ftp://example.com",
            "javascript:alert(1)",
            "tel:+15551234567",
            "data:text/plain,hi",
            "https://",
        ],
    )
    def test_non_web_urls_are_rejected(self, url: str) -> None:
        with pytest.raises(ValueError, match="not a usable web URL"):
            Candidate(url, GEN)

    def test_error_message_names_the_offending_value(self) -> None:
        with pytest.raises(ValueError, match=r"ftp://example\.com"):
            Candidate("ftp://example.com", GEN)

    def test_error_message_names_the_generator(self) -> None:
        # Which origin produced the bad URL is the first thing a reader
        # of the log needs (CLAUDE.md §6).
        with pytest.raises(ValueError, match=r"brave_search"):
            Candidate("ftp://example.com", "brave_search")

    @pytest.mark.parametrize(
        "url",
        [
            "example.com",
            "https://example.com/path",
            "not a url at all",
            "mailto:sales@example.com",
            "https://",
        ],
    )
    def test_validity_is_defined_only_by_unit_1(self, url: str) -> None:
        """Candidate accepts exactly what normalize_url accepts.

        Asserted as an equivalence rather than a fixed verdict per URL, so
        this stays true if Unit 1 tightens its rules. Unit 6 must add no
        host rules of its own: a second definition of "valid URL" here
        would disagree with FieldEvidence and DuplicateURLFilter, which
        consult the same normalizer — so a URL blocked here would still
        reach the crawl queue through the filter.
        """
        normalized = normalize_url(url)
        if normalized is None:
            with pytest.raises(ValueError, match="not a usable web URL"):
                Candidate(url, GEN)
        else:
            assert Candidate(url, GEN).url == normalized


class TestGeneratorValidation:
    def test_non_str_raises_type_error(self) -> None:
        with pytest.raises(TypeError, match="Candidate.generator must be a str"):
            Candidate(URL, 123)  # type: ignore[arg-type]

    @pytest.mark.parametrize("generator", ["", "   "])
    def test_empty_raises_value_error(self, generator: str) -> None:
        # An anonymous candidate destroys provenance.
        with pytest.raises(ValueError, match="Candidate.generator is required"):
            Candidate(URL, generator)

    def test_stripped_and_lowercased(self) -> None:
        assert Candidate(URL, "  My_Gen  ").generator == "my_gen"

    def test_case_variants_agree(self) -> None:
        assert Candidate(URL, "BraveSearch").generator == Candidate(
            URL, "bravesearch"
        ).generator


class TestConfidenceCoercion:
    def test_raw_float_is_coerced(self) -> None:
        c = Candidate(URL, GEN, confidence=0.75)
        assert isinstance(c.confidence, Confidence)
        assert c.confidence.score == 0.75

    def test_int_is_coerced(self) -> None:
        assert Candidate(URL, GEN, confidence=1).confidence == Confidence.CERTAIN

    def test_existing_confidence_is_passed_through(self) -> None:
        conf = Confidence(0.5)
        assert Candidate(URL, GEN, confidence=conf).confidence is conf

    @pytest.mark.parametrize("score", [1.5, -0.1, float("nan")])
    def test_invalid_score_propagates_unit_3_value_error(self, score: float) -> None:
        # Out of range means the producer has a bug; clamping would hide it.
        with pytest.raises(ValueError):
            Candidate(URL, GEN, confidence=score)

    @pytest.mark.parametrize("score", ["high", None, True])
    def test_non_numeric_propagates_unit_3_type_error(self, score: object) -> None:
        with pytest.raises(TypeError):
            Candidate(URL, GEN, confidence=score)


class TestAttributes:
    def test_free_form_values_survive(self) -> None:
        c = Candidate(URL, GEN, attributes={"custom": "value"})
        assert c.attributes["custom"] == "value"

    def test_convention_keys_round_trip(self) -> None:
        attrs = {"title": "Acme", "snippet": "Roofing co", "query": "roofing dallas"}
        assert Candidate(URL, GEN, attributes=attrs).attributes == attrs


class TestImmutability:
    @pytest.mark.parametrize(
        ("attribute", "value"),
        [
            ("url", "https://other.com"),
            ("generator", "other"),
            ("confidence", Confidence(0.5)),
            ("attributes", {}),
        ],
    )
    def test_assignment_raises(self, attribute: str, value: object) -> None:
        c = Candidate(URL, GEN)
        with pytest.raises(FrozenInstanceError):
            setattr(c, attribute, value)

    def test_is_not_hashable(self) -> None:
        # attributes is a mapping; use .key for identity instead.
        with pytest.raises(TypeError):
            hash(Candidate(URL, GEN))

    def test_the_attributes_mapping_is_stored_by_reference(self) -> None:
        """Documents a known limit shared with ExtractionResult.

        frozen=True stops rebinding the field, not mutation of the object
        it points at. Callers must not retain the dict they pass in.
        """
        attrs = {"title": "Acme"}
        c = Candidate(URL, GEN, attributes=attrs)
        attrs["title"] = "Changed"
        assert c.attributes["title"] == "Changed"


class TestCandidateKey:
    def test_key_matches_canonical_key(self) -> None:
        c = Candidate("https://example.com/path", GEN)
        assert c.key == canonical_key("https://example.com/path")

    def test_key_is_never_none(self) -> None:
        assert Candidate(URL, GEN).key is not None

    def test_scheme_www_and_trailing_slash_share_one_key(self) -> None:
        keys = {
            Candidate(url, GEN).key
            for url in (
                "https://example.com",
                "http://example.com/",
                "https://www.example.com",
                "http://www.example.com/",
            )
        }
        assert len(keys) == 1

    def test_different_hosts_have_different_keys(self) -> None:
        assert Candidate(URL, GEN).key != Candidate("https://other.com", GEN).key

    def test_the_generator_does_not_affect_the_key(self) -> None:
        # Identity is the page, so cross-generator dedup works.
        assert Candidate(URL, "a").key == Candidate(URL, "b").key

    def test_host_matches_extract_host(self) -> None:
        c = Candidate("https://example.com/path", GEN)
        assert c.host == extract_host("https://example.com/path")

    def test_host_is_never_none(self) -> None:
        assert Candidate(URL, GEN).host is not None

    def test_host_drops_a_non_default_port(self) -> None:
        assert Candidate("https://example.com:8443/x", GEN).host == "example.com"


class TestSerialization:
    def test_to_dict_has_exactly_four_keys(self) -> None:
        c = Candidate(URL, GEN, confidence=0.8, attributes={"k": "v"})
        assert set(c.to_dict()) == {"url", "generator", "confidence", "attributes"}

    def test_to_dict_reports_the_normalized_url(self) -> None:
        assert Candidate("EXAMPLE.com", GEN).to_dict()["url"] == "https://example.com/"

    def test_to_dict_reports_the_normalized_generator(self) -> None:
        assert Candidate(URL, "  My_Gen  ").to_dict()["generator"] == "my_gen"

    def test_confidence_serializes_as_a_float(self) -> None:
        value = Candidate(URL, GEN, confidence=0.8).to_dict()["confidence"]
        assert isinstance(value, float)
        assert value == 0.8

    def test_default_confidence_serializes_as_zero(self) -> None:
        assert Candidate(URL, GEN).to_dict()["confidence"] == 0.0

    def test_attributes_are_copied_not_aliased(self) -> None:
        c = Candidate(URL, GEN, attributes={"k": "v"})
        c.to_dict()["attributes"]["k"] = "modified"
        assert c.attributes["k"] == "v"

    def test_str_names_url_generator_and_confidence(self) -> None:
        text = str(Candidate(URL, GEN, confidence=0.5))
        assert "https://example.com/" in text
        assert GEN in text
        assert "0.50" in text


class TestCandidateGeneratorContract:
    def test_the_base_class_is_abstract(self) -> None:
        with pytest.raises(TypeError):
            CandidateGenerator()  # type: ignore[abstract]

    def test_generate_is_abstract(self) -> None:
        assert "generate" in CandidateGenerator.__abstractmethods__

    def test_a_subclass_implementing_generate_is_concrete(self) -> None:
        gen = make_generator()
        assert isinstance(gen, CandidateGenerator)
        assert gen.generate(industry="Roofing", location="Dallas", limit=10) == []

    def test_generate_is_keyword_only(self) -> None:
        # Matches BaseSource.discover and BaseDiscoveryPlugin.discover.
        parameters = inspect.signature(CandidateGenerator.generate).parameters
        for name in ("industry", "location", "limit"):
            assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY

    def test_calling_the_abstract_generate_raises_not_implemented(self) -> None:
        class Impl(CandidateGenerator):
            def generate(self, *, industry, location, limit):
                return super().generate(
                    industry=industry, location=location, limit=limit
                )

        with pytest.raises(NotImplementedError):
            Impl().generate(industry="Roofing", location="Dallas", limit=1)

    def test_generator_name_defaults_to_the_class_name(self) -> None:
        assert make_generator("MyGen").generator_name == "MyGen"

    def test_generator_name_honours_an_explicit_name(self) -> None:
        assert make_generator("MyGen", name="custom_name").generator_name == (
            "custom_name"
        )

    def test_describe_reports_name_and_class(self) -> None:
        assert make_generator("MyGen").describe() == {
            "name": "MyGen",
            "class": "MyGen",
        }

    def test_describe_distinguishes_name_from_class(self) -> None:
        assert make_generator("MyGen", name="brave_search").describe() == {
            "name": "brave_search",
            "class": "MyGen",
        }

    def test_repr_names_the_generator(self) -> None:
        assert repr(make_generator("MyGen")) == "<MyGen name='MyGen'>"

    def test_a_subclass_can_return_candidates(self) -> None:
        class Impl(CandidateGenerator):
            name = "seed_list"

            def generate(self, *, industry, location, limit):
                return [
                    Candidate(url="example.com", generator=self.generator_name)
                ][:limit]

        results = Impl().generate(industry="Roofing", location="Dallas", limit=5)
        assert len(results) == 1
        assert results[0].generator == "seed_list"
        assert results[0].url == "https://example.com/"


class TestNoImplementation:
    """Phase 2.3A adds no fetching, parsing, scoring or filtering."""

    @pytest.mark.parametrize(
        "name",
        ["requests", "aiohttp", "httpx", "BeautifulSoup", "bs4", "selectolax", "re"],
    )
    def test_the_module_pulls_in_no_http_or_parsing_library(self, name: str) -> None:
        from app.discovery.website import candidates as module

        assert name not in vars(module)

    @pytest.mark.parametrize(
        "name",
        ["fetch", "crawl", "download", "search", "rank", "score", "filter", "dedupe"],
    )
    def test_the_contract_declares_no_forbidden_operation(self, name: str) -> None:
        from app.discovery.website import candidates as module

        assert not hasattr(module, name)
        assert not hasattr(CandidateGenerator, name)
        assert not hasattr(Candidate, name)

    def test_generate_is_synchronous(self) -> None:
        assert not inspect.iscoroutinefunction(CandidateGenerator.generate)

    @pytest.mark.parametrize(
        "name",
        ["url_filter", "DuplicateURLFilter", "crawlers", "sources", "SourceStatus"],
    )
    def test_the_module_stays_a_leaf(self, name: str) -> None:
        # Units 1 and 3 only. Unit 2 is wired in by Unit 8, not here.
        from app.discovery.website import candidates as module

        assert name not in vars(module)

    def test_no_concrete_generator_ships_in_this_unit(self) -> None:
        # Scoped to the module's own namespace: __subclasses__() would
        # also see the throwaway classes these tests define.
        from app.discovery.website import candidates as module

        concrete = [
            name
            for name, obj in vars(module).items()
            if isinstance(obj, type)
            and issubclass(obj, CandidateGenerator)
            and not inspect.isabstract(obj)
        ]
        assert concrete == []

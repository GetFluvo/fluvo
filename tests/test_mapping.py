"""Tests for the declarative mapping compiler (#352)."""

from typing import Any

import polars as pl
import pytest

from fluvo.lib import mapping as m
from fluvo.lib.core import InputError
from fluvo.lib.flow import FlowError

_BODY: dict[str, Any] = {
    "version": 1,
    "model": "res.partner",
    "id": {"prefix": "mig_partner", "from": "legacy_id"},
    "columns": {
        "name": {"copy": "Name"},
        "active": {"const": "1"},
        "parent_id/id": {"xmlid": {"prefix": "mig_partner", "from": "parent_ref"}},
        "type": {
            "substitute": {
                "from": "Kind",
                "map": {"C": "contact", "I": "invoice"},
                "on_missing": "error",
            }
        },
    },
    "ignore": ["Notes"],
}


def _spec(**overrides: Any) -> m.MappingSpec:
    """Parse a copy of the sample body, applying overrides to the top level."""
    body = {**_BODY, **overrides}
    return m.parse_mapping(body, where="flow 'f', step 1, mapping")


class TestParseMapping:
    """Structural validation (FlowError) at flow-load time."""

    def test_parses_a_valid_mapping(self) -> None:
        """A well-formed body yields a MappingSpec with ordered columns."""
        spec = _spec()
        assert spec.model == "res.partner"
        assert spec.id_prefix == "mig_partner" and spec.id_from == "legacy_id"
        assert list(spec.columns) == ["name", "active", "parent_id/id", "type"]
        assert spec.ignore == ("Notes",)

    def test_rejects_wrong_version(self) -> None:
        """Only version 1 is accepted."""
        with pytest.raises(FlowError, match="version must be 1"):
            _spec(version=2)

    def test_requires_model(self) -> None:
        """`model` is mandatory."""
        with pytest.raises(FlowError, match="`model` is required"):
            _spec(model="")

    def test_requires_id_block(self) -> None:
        """The id block needs both prefix and from."""
        with pytest.raises(FlowError, match="`id` needs `prefix` and `from`"):
            _spec(id={"prefix": "x"})

    def test_unknown_top_key_rejected(self) -> None:
        """An unknown top-level key is a hard error."""
        with pytest.raises(FlowError, match="unknown key 'extra'"):
            _spec(extra=1)

    def test_reserved_id_column_rejected(self) -> None:
        """A column literally named 'id' collides with the id block."""
        cols = {**_BODY["columns"], "id": {"copy": "X"}}
        with pytest.raises(FlowError, match="column 'id' is reserved"):
            _spec(columns=cols)

    def test_two_primitives_on_one_column_rejected(self) -> None:
        """A column must carry exactly one primitive."""
        cols = {"name": {"copy": "Name", "const": "x"}}
        with pytest.raises(FlowError, match="exactly one primitive"):
            _spec(columns=cols)

    def test_unknown_primitive_rejected(self) -> None:
        """An unknown primitive name is rejected (relation/py are deferred)."""
        cols = {"street": {"relation": {"from": "X"}}}
        with pytest.raises(FlowError, match="unknown primitive 'relation'"):
            _spec(columns=cols)

    def test_bad_on_missing_rejected(self) -> None:
        """substitute.on_missing must be one of the three policies."""
        cols = {"type": {"substitute": {"from": "K", "map": {}, "on_missing": "guess"}}}
        with pytest.raises(FlowError, match="on_missing must be error|blank|keep"):
            _spec(columns=cols)


class TestValidateAgainstSource:
    """Data validation (InputError) at step start."""

    def _df(self, **cols: list[str]) -> pl.DataFrame:
        return pl.DataFrame(cols, schema={c: pl.String for c in cols})

    def test_passes_for_a_matching_source(self) -> None:
        """A source covering every mapped/ignored column validates cleanly."""
        df = self._df(
            legacy_id=["1"],
            Name=["Acme"],
            parent_ref=["9"],
            Kind=["C"],
            Notes=["x"],
        )
        m.validate_against_source(_spec(), df)  # no raise

    def test_missing_referenced_column(self) -> None:
        """A mapping naming a column absent from the source fails."""
        df = self._df(legacy_id=["1"], Name=["Acme"], Kind=["C"], Notes=["x"])
        with pytest.raises(InputError, match="not in the file.*parent_ref"):
            m.validate_against_source(_spec(), df)

    def test_unaccounted_source_column(self) -> None:
        """A source column neither mapped nor ignored fails (nothing silent)."""
        df = self._df(
            legacy_id=["1"],
            Name=["Acme"],
            parent_ref=["9"],
            Kind=["C"],
            Notes=["x"],
            Extra=["leftover"],
        )
        with pytest.raises(InputError, match="neither mapped nor ignored.*Extra"):
            m.validate_against_source(_spec(), df)

    def test_empty_id_key(self) -> None:
        """An empty id source value is rejected before any load."""
        df = self._df(
            legacy_id=["1", ""],
            Name=["a", "b"],
            parent_ref=["9", "9"],
            Kind=["C", "I"],
            Notes=["", ""],
        )
        with pytest.raises(InputError, match="empty `legacy_id`"):
            m.validate_against_source(_spec(), df)

    def test_substitute_error_unmapped_value(self) -> None:
        """An on_missing=error substitution must cover every value seen."""
        df = self._df(
            legacy_id=["1"],
            Name=["a"],
            parent_ref=["9"],
            Kind=["Z"],  # not in {C, I}
            Notes=[""],
        )
        with pytest.raises(InputError, match="unmapped value.*Z"):
            m.validate_against_source(_spec(), df)


class TestCompileMapping:
    """The compiled dict reproduces the expected mapper behaviour."""

    def test_each_primitive_compiles(self) -> None:
        """copy/const/xmlid/substitute compile to funcs with the right output."""
        compiled = m.compile_mapping(_spec())
        assert list(compiled) == ["id", "name", "active", "parent_id/id", "type"]
        line = {
            "legacy_id": "42",
            "Name": "Acme",
            "parent_ref": "7",
            "Kind": "C",
        }
        state: dict[str, Any] = {}
        assert compiled["id"](line, state) == "mig_partner.42"
        assert compiled["name"](line, state) == "Acme"
        assert compiled["active"](line, state) == "1"
        assert compiled["parent_id/id"](line, state) == "mig_partner.7"
        assert compiled["type"](line, state) == "contact"

    def test_substitute_blank_and_keep(self) -> None:
        """Blank yields '' on a miss; keep returns the original value."""
        blank = m._substitute({"C": "contact"}, "Kind", "blank")
        keep = m._substitute({"C": "contact"}, "Kind", "keep")
        assert blank({"Kind": "Z"}, {}) == ""
        assert keep({"Kind": "Z"}, {}) == "Z"
        assert keep({"Kind": "C"}, {}) == "contact"

    def test_substitute_error_raises_on_miss(self) -> None:
        """An error substitution raises rather than emit a wrong value."""
        strict = m._substitute({"C": "contact"}, "Kind", "error")
        with pytest.raises(InputError, match="unmapped value 'Z'"):
            strict({"Kind": "Z"}, {})

    def test_end_to_end_through_processor(self) -> None:
        """The compiled mapping transforms a frame to the expected columns."""
        from fluvo.lib.transform import Processor

        df = pl.DataFrame(
            {
                "legacy_id": ["1", "2"],
                "Name": ["Acme", "Globex"],
                "parent_ref": ["9", "9"],
                "Kind": ["C", "I"],
                "Notes": ["x", "y"],
            },
            schema={
                c: pl.String
                for c in ("legacy_id", "Name", "parent_ref", "Kind", "Notes")
            },
        )
        spec = _spec()
        m.validate_against_source(spec, df)
        compiled = m.compile_mapping(spec)
        out = Processor(dataframe=df, mapping=compiled, separator=";").process(
            filename_out=""
        )
        assert set(out.columns) == {"id", "name", "active", "parent_id/id", "type"}
        assert out.get_column("id").to_list() == ["mig_partner.1", "mig_partner.2"]
        assert out.get_column("type").to_list() == ["contact", "invoice"]
        assert out.get_column("active").to_list() == ["1", "1"]

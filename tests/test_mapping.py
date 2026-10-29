from types import SimpleNamespace

from app.demo_data import NS_HEADERS, RB_HEADERS
from app.services.mapping import LLMMapper, RuleBasedMapper, detect_transform, propose_mapping
from app.services.template import load_template

from .conftest import sample

T = load_template()


def test_offline_matcher_maps_every_northstar_column():
    _, parsed = sample("northstar_q2_v1")
    proposals = RuleBasedMapper().propose(parsed.columns, parsed.rows, T)
    mapped = {c: p.field for c, p in proposals.items()}
    assert set(mapped.values()) - {None} == set(T.by_key)  # all 16 fields fed
    assert mapped["Internal comment"] is None
    assert proposals["Net Asset Val (EUR k)"].transform == "thousands"
    assert len(NS_HEADERS) == 17


def test_offline_matcher_handles_french_headers_and_fractions():
    _, parsed = sample("riverbend_q2")
    proposals = RuleBasedMapper().propose(parsed.columns, parsed.rows, T)
    assert all(p.field for p in proposals.values()), len(RB_HEADERS)
    assert proposals["Actif net (milliers EUR)"].transform == "thousands"
    assert proposals["Liquidité 1 jour"].transform == "fraction_to_percent"
    assert proposals["Top 5 investisseurs (%)"].transform == "none"  # real percentages, not fractions


def test_detect_transform():
    rows = [{"x": 0.2}, {"x": 0.8}]
    assert detect_transform("x", "percent", rows)[0] == "fraction_to_percent"
    assert detect_transform("NAV (EUR m)", "amount", [])[0] == "millions"
    assert detect_transform("NAV", "amount", [])[0] == "none"


def test_saved_mappings_are_reused_for_the_same_client(db, clients):
    # Riverbend's columns were confirmed during seeding.
    _, parsed = sample("riverbend_q2")
    proposals, method = propose_mapping(db, clients["RIVERBEND"].id, parsed.columns, parsed.rows, T)
    assert method == "saved"
    assert all(p.source == "saved" for p in proposals.values())


def test_saved_mappings_do_not_leak_across_clients(db, clients):
    _, parsed = sample("riverbend_q2")
    proposals, method = propose_mapping(
        db, clients["NORTHSTAR"].id, parsed.columns, parsed.rows, T, mapper=RuleBasedMapper()
    )
    assert method == "rules"
    assert not any(p.source == "saved" for p in proposals.values())


class FakeAnthropic:
    def __init__(self, mappings):
        self.mappings, self.messages, self.request = mappings, self, None

    def create(self, **kwargs):
        self.request = kwargs
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input={"mappings": self.mappings})])


def test_ai_mapper_validates_output():
    fake = FakeAnthropic(
        [
            {"column": "Fonds", "field": "fund_name", "transform": "none", "confidence": 0.9, "reason": "Name"},
            {"column": "VNI (k)", "field": "nav", "transform": "thousands", "confidence": 0.8, "reason": "NAV"},
            {"column": "Autre", "field": "nav", "transform": "none", "confidence": 0.5, "reason": "dup"},
            {"column": "Ghost", "field": "nav", "transform": "none", "confidence": 1, "reason": "not a column"},
            {"column": "Zut", "field": "made_up", "transform": "weird", "confidence": 0.7, "reason": "bad"},
        ]
    )
    columns = ["Fonds", "VNI (k)", "Autre", "Zut", "Vide"]
    proposals = LLMMapper(client=fake, model="test").propose(columns, [{"Fonds": "A"}], T)
    assert proposals["VNI (k)"].field == "nav" and proposals["VNI (k)"].transform == "thousands"
    assert proposals["Autre"].field is None  # a field is fed by one column only
    assert proposals["Zut"].field is None and proposals["Zut"].transform == "none"
    assert proposals["Vide"].field is None  # columns the AI skipped are still listed
    assert "Ghost" not in proposals
    assert fake.request["tool_choice"] == {"type": "tool", "name": "propose_mapping"}

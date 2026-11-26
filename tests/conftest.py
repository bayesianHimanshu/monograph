from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings

from monograph import wiring
from monograph.core.instances import Instantiator
from monograph.core.models import MODELS
from monograph.core.spec import SchemaSpec, analyze
from monograph.core.version import SCHEMA_VERSION
from monograph.core.vocab import Vocabulary
from monograph.rdf.mapping import RdfMapper
from monograph.validate.agreement import AgreementChecker
from monograph.validate.fixtures import FixtureLoader

TODAY = date(2026, 11, 26)


@pytest.fixture(scope="session")
def spec() -> SchemaSpec:
    return analyze(MODELS, SCHEMA_VERSION)


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def vocab() -> Vocabulary:
    return Vocabulary()


@pytest.fixture(scope="session")
def instantiator(spec: SchemaSpec) -> Instantiator:
    return Instantiator(spec)


@pytest.fixture(scope="session")
def mapper(spec: SchemaSpec, vocab: Vocabulary) -> RdfMapper:
    return wiring.mapper(spec, vocab)


@pytest.fixture(scope="session")
def checker(spec: SchemaSpec) -> AgreementChecker:
    return wiring.agreement_checker(spec, ROOT, TODAY)


@pytest.fixture(scope="session")
def fixtures(spec: SchemaSpec) -> FixtureLoader:
    return FixtureLoader(spec, wiring.fixtures_dir(ROOT))


settings.register_profile("ci", max_examples=40, deadline=None, suppress_health_check=list(HealthCheck))
settings.register_profile("mutation", max_examples=10, deadline=None, suppress_health_check=list(HealthCheck))
settings.register_profile("deep", max_examples=400, deadline=None, suppress_health_check=list(HealthCheck))
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))

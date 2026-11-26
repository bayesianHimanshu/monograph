from __future__ import annotations

from datetime import date

import pytest

from monograph.core.models import MODELS
from monograph.core.spec import SchemaSpec, analyze
from monograph.core.version import SCHEMA_VERSION

TODAY = date(2026, 11, 26)


@pytest.fixture(scope="session")
def spec() -> SchemaSpec:
    return analyze(MODELS, SCHEMA_VERSION)

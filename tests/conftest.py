"""Environment defaults for the test suite.

`pipeline.config` validates at import, so these must be set before any test module
imports it. Real environment variables still win — this only fills the gaps.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://pipeline:pipeline@localhost:5432/pipeline")
os.environ.setdefault("LOG_LEVEL", "INFO")

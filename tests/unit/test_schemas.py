"""JSON Schema v1 contract tests.

Requires: python3 -m pip install jsonschema
          (or: python3 -m pip install -r requirements-dev.txt)
Run:     python3 -m unittest tests/unit/test_schemas.py
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "shared" / "schema"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "schemas"

HASH_FIELDS = (
    "file_id",
    "src_in",
    "src_out",
    "audio_stream_index",
    "channel_map",
    "model_name",
    "weights_sha256",
    "vocals_only_bag",
    "wet",
    "gain",
    "mono",
    "handles_requested",
    "file_duration_seconds",
    "segment",
    "overlap",
    "shifts",
    "enhancer_id",
    "project_sample_rate",
    "sample_format",
    "resampler_id",
    "clip_policy",
    "engine_semver",
)


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _registry() -> Registry:
    resources = []
    for path in sorted(SCHEMA_DIR.glob("*.json")):
        contents = _load_json(path)
        if isinstance(contents, dict) and "$id" in contents:
            resources.append((contents["$id"], Resource.from_contents(contents)))
    return Registry().with_resources(resources)


def _schema(name: str) -> dict:
    return _load_json(SCHEMA_DIR / name)


def _fixture(name: str) -> object:
    return _load_json(FIXTURE_DIR / name)


def _validator(schema: dict) -> Draft202012Validator:
    return Draft202012Validator(
        schema,
        registry=_registry(),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


class SchemaContractTests(unittest.TestCase):
    def assert_valid(self, schema_name: str, fixture_name: str) -> None:
        _validator(_schema(schema_name)).validate(_fixture(fixture_name))

    def assert_invalid(
        self, schema_name: str, fixture_name: str, *needles: str
    ) -> None:
        with self.assertRaises(ValidationError) as ctx:
            _validator(_schema(schema_name)).validate(_fixture(fixture_name))
        blob = " ".join(
            [
                str(ctx.exception.message),
                " ".join(str(p) for p in ctx.exception.absolute_path),
                " ".join(str(p) for p in ctx.exception.absolute_schema_path),
            ]
        )
        for needle in needles:
            self.assertIn(needle, blob)

    def test_valid_clip_fixture(self) -> None:
        self.assert_valid("clip.v1.json", "clip.valid.json")

    def test_valid_params_fixture(self) -> None:
        self.assert_valid("params.v1.json", "params.valid.json")

    def test_valid_job_fixture(self) -> None:
        self.assert_valid("job.v1.json", "job.valid.json")

    def test_params_wet_dry_sample_rate_must_fail(self) -> None:
        self.assert_invalid(
            "params.v1.json",
            "params.wet_dry_sample_rate.json",
            "wet_dry_sample_rate",
        )

    def test_source_channels_6_must_fail(self) -> None:
        self.assert_invalid(
            "clip.v1.json",
            "clip.source_channels_6.json",
            "source_channels",
        )

    def test_missing_allowed_roots_must_fail(self) -> None:
        self.assert_invalid(
            "params.v1.json",
            "params.missing_allowed_roots.json",
            "allowed_roots",
        )

    def test_rational_fps_missing_den_must_fail(self) -> None:
        self.assert_invalid(
            "clip.v1.json",
            "clip.fps_missing_den.json",
            "den",
        )

    def test_job_missing_handles_left_actual_must_fail(self) -> None:
        self.assert_invalid(
            "job.v1.json",
            "job.missing_handles_left_actual.json",
            "handles_left_actual",
        )

    def test_job_params_ref_is_relative(self) -> None:
        schema = _schema("job.v1.json")
        self.assertEqual(schema["properties"]["params"]["$ref"], "params.v1.json")

    def test_hash_fields_list_is_normative(self) -> None:
        schema = _schema("hash-fields.v1.json")
        self.assertEqual(tuple(schema["required"]), HASH_FIELDS)
        self.assertEqual(set(schema["properties"]), set(HASH_FIELDS))
        self.assertNotIn("wet_dry_sample_rate", schema["properties"])
        self.assertNotIn("wet_dry_sample_rate", schema["required"])
        self.assertFalse(schema.get("additionalProperties", True))


if __name__ == "__main__":
    unittest.main()

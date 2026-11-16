"""Pure byte-stream safety checks for opt-in baseline payload deferral."""
from __future__ import annotations

import io
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from deferred_payload import (
    ARTICLE_COLUMNS, PAYLOAD_COLUMNS, BaselinePayloadTransform,
    DeferredPayloadError, HYDRATION_TABLE, TRACKING_TABLE, deferred_payload_enabled,
)


def quote(value: bytes) -> bytes:
    return b"'" + value.replace(b"\\", b"\\\\").replace(b"'", b"''") + b"'"


def article_schema() -> bytes:
    return (b"CREATE TABLE `ojs_articles` (\n" + b"".join(
        b"  `" + name.encode() + b"` mediumtext NULL,\n" for name in ARTICLE_COLUMNS
    ) + b"  PRIMARY KEY (`article_id`)\n) ENGINE=InnoDB;\n")


def row_values(article_id=1, payload=b"payload"):
    values = [str(article_id).encode()]
    values.extend(quote(f"scalar-{index}".encode()) for index in range(1, len(ARTICLE_COLUMNS)))
    for index, name in enumerate(ARTICLE_COLUMNS):
        if name in PAYLOAD_COLUMNS:
            values[index] = quote(payload + b":" + name.encode())
    values[ARTICLE_COLUMNS.index("subjects")] = b"NULL"
    values[ARTICLE_COLUMNS.index("rights")] = b"''"
    values[ARTICLE_COLUMNS.index("canonical_metadata_hash")] = b"0x00ffAB"
    return values


def insert(rows) -> bytes:
    return b"INSERT INTO `ojs_articles` VALUES " + b",".join(
        b"(" + b",".join(values) + b")" for values in rows
    ) + b";\n"


def transform(data: bytes, *, hydrate=False, limit=65536, **options):
    transformer = BaselinePayloadTransform(hydrate=hydrate, **options)
    source = io.BytesIO(data)
    output = []
    while chunk := source.readline(limit):
        output.append(transformer.transform(chunk))
        if len(transformer.token) > 4096:
            raise AssertionError("scalar buffering exceeded its bounded limit")
    output.append(transformer.finish())
    return b"".join(output), transformer


class DeferredPayloadSafetyTests(unittest.TestCase):
    def test_configuration_requires_explicit_boolean_and_defaults_off(self):
        self.assertFalse(deferred_payload_enabled({}))
        self.assertFalse(deferred_payload_enabled({"OJS_DEFER_BASELINE_PAYLOAD": "0"}))
        self.assertTrue(deferred_payload_enabled({"OJS_DEFER_BASELINE_PAYLOAD": "1"}))
        for value in ("", "true", "yes", "01", " 1", "1\n", 1, None):
            with self.subTest(value=value), self.assertRaises(DeferredPayloadError):
                deferred_payload_enabled({"OJS_DEFER_BASELINE_PAYLOAD": value})

    def test_defer_changes_only_payload_tokens_including_null_and_empty(self):
        rows = [row_values(1), row_values(2, b"other")]
        original = article_schema() + insert(rows)
        expected = article_schema() + insert([
            [b"NULL" if name in PAYLOAD_COLUMNS else token
             for name, token in zip(ARTICLE_COLUMNS, values)] for values in rows
        ])
        for limit in (64, 65, 97, 65536):
            with self.subTest(limit=limit):
                output, parser = transform(original, limit=limit)
                self.assertEqual(output, expected)
                self.assertEqual(parser.rows, 2)

    def test_quotes_escapes_unicode_binary_and_fake_sql_are_lossless_across_fragments(self):
        special = ("é界🙂".encode() + b" ' \\ '' , ); -- comment\n"
                   b"CREATE TABLE `ojs_articles` (\n"
                   b"INSERT INTO `ojs_articles` VALUES (999);\n"
                   b"\x00\xff # /* */ end")
        for padding in range(70):
            values = row_values(payload=b"x" * padding + special)
            original = article_schema() + insert([values])
            selected = [values[0]] + [values[ARTICLE_COLUMNS.index(name)] for name in PAYLOAD_COLUMNS]
            expected_tuple = b"(" + b",".join(selected) + b")"
            with self.subTest(padding=padding):
                output, parser = transform(original, hydrate=True, limit=64)
                self.assertIn(expected_tuple, output)
                self.assertEqual(parser.rows, 1)
                self.assertIn(b"INNER JOIN " + TRACKING_TABLE.encode(), output)
                self.assertTrue(output.endswith(
                    f"DROP TEMPORARY TABLE {HYDRATION_TABLE};\n".encode()))

    def test_other_tables_and_comments_are_preserved_but_never_hydrated(self):
        other = (b"-- CREATE TABLE `ojs_articles` (\n"
                 b"CREATE TABLE `other` (\n  `text` longtext\n) ENGINE=InnoDB;\n"
                 b"INSERT INTO `other` VALUES ('not an article');\n")
        data = other + article_schema() + insert([row_values()])
        deferred, _ = transform(data)
        self.assertTrue(deferred.startswith(other))
        hydrated, _ = transform(data, hydrate=True)
        self.assertNotIn(b"not an article", hydrated)

    def test_hydration_batches_are_bounded_by_rows_and_bytes(self):
        data = article_schema() + insert([row_values(number) for number in range(1, 6)])
        output, _ = transform(data, hydrate=True, batch_rows=2)
        self.assertEqual(output.count(f"INSERT INTO {HYDRATION_TABLE}".encode()), 3)
        self.assertEqual(output.count(f"TRUNCATE TABLE {HYDRATION_TABLE}".encode()), 3)
        output, _ = transform(data, hydrate=True, batch_rows=100, batch_bytes=1)
        self.assertEqual(output.count(f"INSERT INTO {HYDRATION_TABLE}".encode()), 5)
        for kwargs in ({"batch_rows": 0}, {"batch_bytes": 0}, {"batch_rows": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(DeferredPayloadError):
                BaselinePayloadTransform(**kwargs)

    def test_multi_megabyte_value_is_streamed_without_token_buffer_growth(self):
        values = row_values()
        values[ARTICLE_COLUMNS.index("metadata_xml")] = quote(b"x" * (3 * 1024 * 1024))
        data = article_schema() + insert([values])
        output, parser = transform(data, limit=65536)
        self.assertLess(len(output), 10000)
        self.assertEqual(parser.rows, 1)
        hydrated, _ = transform(data, hydrate=True, limit=65536)
        self.assertIn(values[ARTICLE_COLUMNS.index("metadata_xml")], hydrated)

    def test_unknown_schema_or_insert_grammar_fails_closed(self):
        valid = article_schema() + insert([row_values()])
        cases = {
            "missing schema": insert([row_values()]),
            "wrong columns": valid.replace(b"  `title` ", b"  `unrecognized` ", 1),
            "duplicate schema": article_schema() + valid,
            "explicit columns": valid.replace(b"`ojs_articles` VALUES", b"`ojs_articles` (`article_id`) VALUES"),
            "missing value": article_schema() + insert([row_values()[:-1]]),
            "extra value": article_schema() + insert([row_values() + [b"NULL"]]),
            "negative ID": article_schema() + insert([[b"-1"] + row_values()[1:]]),
            "oversized ID": article_schema() + insert([[str(2**64).encode()] + row_values()[1:]]),
            "quoted ID": article_schema() + insert([[b"'1'"] + row_values()[1:]]),
            "SQL expression": article_schema() + insert([[b"NOW()"] + row_values()[1:]]),
            "truncated": valid[:-6],
            "appended SQL": valid.rstrip() + b" SELECT 1;\n",
            "unfinished comment": valid + b"/* unfinished",
        }
        for name, data in cases.items():
            for hydrate in (False, True):
                with self.subTest(case=name, hydrate=hydrate), self.assertRaises(DeferredPayloadError):
                    transform(data, hydrate=hydrate, limit=64)


if __name__ == "__main__":
    unittest.main()

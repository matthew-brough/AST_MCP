"""SPEC §T.12 / §T.13 — the `defs` profile across scripting, web, data, devops."""

import unittest
from pathlib import Path

from ast_mcp.extract import extract
from ast_mcp.languages import BY_LANG, LANGS, spec_for_path
from ast_mcp.parser import parse_file

FIXTURES = Path(__file__).parent / "fixtures"


def run(relative: str):
    parsed, errors = parse_file(FIXTURES / relative)
    assert parsed is not None, errors
    extraction = extract(parsed)
    return parsed, extraction, {s.qualified_name: s for s in extraction.symbols}


class TestRegistryBreadth(unittest.TestCase):
    def test_twenty_six_rows_over_six_groups(self):
        self.assertEqual(len(LANGS), 26)
        self.assertEqual(
            {s.group for s in LANGS},
            {"core", "web", "scripting", "data", "devops", "docs"},
        )

    def test_dockerfile_matches_by_filename(self):
        for name in ("Dockerfile", "Containerfile", "build/Dockerfile"):
            with self.subTest(name=name):
                spec = spec_for_path(name)
                self.assertIsNotNone(spec)
                self.assertEqual(spec.lang, "dockerfile")

    def test_profile_assignments(self):
        self.assertEqual(BY_LANG["yaml"].profile, "schema")
        self.assertEqual(BY_LANG["markdown"].profile, "outline")
        self.assertEqual(BY_LANG["html"].profile, "outline")
        self.assertEqual(BY_LANG["sql"].profile, "defs")
        self.assertEqual(BY_LANG["python"].profile, "symbols")


class TestScripting(unittest.TestCase):
    def test_ruby_nesting_and_comments_on_the_grandparent(self):
        _, _ex, symbols = run("scripting/sample.rb")
        self.assertEqual(
            set(symbols),
            {"Billing", "Billing.Invoice", "Billing.Invoice.total", "Billing.Invoice.build"},
        )
        self.assertEqual(symbols["Billing.Invoice"].docstring, "# An invoice.")
        self.assertEqual(symbols["Billing.Invoice.total"].docstring, "# Returns the total.")

    def test_ruby_signature_stops_before_a_trailing_comment(self):
        _, _ex, symbols = run("scripting/sample.rb")
        self.assertEqual(symbols["Billing"].signature, "module Billing")
        self.assertNotIn("#", symbols["Billing"].signature)

    def test_perl_package_and_subs(self):
        _, ex, symbols = run("scripting/sample.pl")
        self.assertEqual(symbols["Widget"].kind, "module")
        self.assertEqual(symbols["build"].docstring, "# Builds a widget.")
        self.assertEqual({i.module for i in ex.imports}, {"strict", "POSIX"})

    def test_r_roxygen_and_library_call(self):
        _, ex, symbols = run("scripting/sample.R")
        self.assertIn("add", symbols)
        self.assertIn("scale_it", symbols)
        self.assertTrue(symbols["add"].docstring.startswith("#' Adds two numbers."))
        self.assertEqual([i.module for i in ex.imports], ["dplyr"])

    def test_bash_source_is_an_import_not_an_alias(self):
        for name in ("scripting/sample.sh", "scripting/sample.zsh"):
            with self.subTest(name=name):
                _, ex, symbols = run(name)
                self.assertEqual(symbols["greet"].docstring, "# Greets the user.")
                self.assertEqual(symbols["APP_ENV"].kind, "var")
                imported = ex.imports[0]
                self.assertEqual(imported.module, "./lib.sh")
                self.assertIsNone(imported.alias)


class TestWeb(unittest.TestCase):
    def test_css_selectors_are_the_names(self):
        _, ex, symbols = run("web/sample.css")
        self.assertIn(".btn, #main .btn:hover", symbols)
        self.assertEqual(symbols[".btn, #main .btn:hover"].kind, "rule")
        self.assertEqual(
            symbols[".btn, #main .btn:hover"].docstring, "/* Primary button. */"
        )
        self.assertEqual([i.module for i in ex.imports], ["reset.css"])

    def test_scss_mixin_and_function(self):
        _, _ex, symbols = run("web/sample.scss")
        self.assertEqual(symbols["flex-center"].kind, "function")
        self.assertEqual(symbols["double"].kind, "function")
        self.assertEqual(symbols[".card"].kind, "rule")


class TestSchemaLanguages(unittest.TestCase):
    def test_sql_ddl_kinds(self):
        _, _ex, symbols = run("data/schema.sql")
        self.assertEqual(symbols["users"].kind, "table")
        self.assertEqual(symbols["active_users"].kind, "view")
        self.assertEqual(symbols["users_email_idx"].kind, "index")

    def test_graphql_definitions(self):
        _, _ex, symbols = run("data/schema.graphql")
        self.assertEqual(symbols["Node"].kind, "interface")
        self.assertEqual(symbols["Role"].kind, "enum")
        self.assertIn("DateTime", symbols)

    def test_proto_nests_rpc_under_service(self):
        _, ex, symbols = run("data/schema.proto")
        self.assertEqual(symbols["User"].kind, "message")
        self.assertEqual(symbols["UserService.GetUser"].kind, "rpc")
        self.assertEqual(symbols["UserService.GetUser"].parent, "UserService")
        self.assertEqual(
            [i.module for i in ex.imports], ["google/protobuf/timestamp.proto"]
        )


class TestDevOps(unittest.TestCase):
    def test_terraform_block_labels_form_the_name(self):
        _, _ex, symbols = run("devops/main.tf")
        self.assertIn("aws_s3_bucket.assets", symbols)
        self.assertIn("region", symbols)
        self.assertEqual(symbols["aws_s3_bucket.assets"].docstring, "# Primary bucket.")
        self.assertTrue(
            symbols["region"].signature.startswith('variable "region"'),
            "the block keyword stays visible in the signature",
        )

    def test_dockerfile_stages_and_image_edges(self):
        _, ex, symbols = run("devops/Dockerfile")
        self.assertEqual(set(symbols), {"build", "runtime"})
        self.assertEqual(symbols["build"].kind, "stage")
        self.assertEqual(symbols["build"].docstring, "# Build stage.")
        kinds = {(i.kind, i.module) for i in ex.imports}
        self.assertIn(("base_image", "node:20-alpine"), kinds)
        self.assertIn(("base_image", "alpine:3.19"), kinds)
        self.assertIn(("copy_from", "--from=build"), kinds)


if __name__ == "__main__":
    unittest.main()

"""Tests for the selector cascade engine, verifier row selection, and CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import get_settings
from src.models import MatchStatus, TunnelStatus
from src.utils.selectors import (
    REQUIRED_SELECTOR_PATHS,
    SelectorCascade,
    SelectorNotFound,
    SelectorRegistry,
)
from src.verify_and_correct import (
    build_where_clause,
    needs_booking_retry,
    needs_tunnel_retry,
)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "selectors.yaml"


@pytest.fixture(scope="module")
def registry() -> SelectorRegistry:
    return SelectorRegistry.load(CONFIG_PATH)


class TestSelectorRegistry:
    def test_all_required_paths_exist(self, registry: SelectorRegistry) -> None:
        assert registry.validate(REQUIRED_SELECTOR_PATHS) == []

    def test_missing_path_raises(self, registry: SelectorRegistry) -> None:
        with pytest.raises(SelectorNotFound):
            registry.get("nuitee.does.not.exist")

    def test_optional_path_returns_empty(self, registry: SelectorRegistry) -> None:
        assert registry.get_optional("nuitee.nope").candidates == []

    def test_cascades_are_ordered_and_non_empty(
        self, registry: SelectorRegistry
    ) -> None:
        cascade = registry.get("nuitee.tunnel.summary.total_price")
        assert len(cascade) >= 3
        # Most specific first: a data-testid should lead.
        assert "data-testid" in cascade.candidates[0]

    def test_placeholder_substitution(self, registry: SelectorRegistry) -> None:
        rendered = registry.get("nuitee.calendar_day").format(iso="2026-08-01")
        assert "2026-08-01" in rendered.candidates[0]
        assert "{iso}" not in rendered.combined

    def test_literal_braces_survive_formatting(self) -> None:
        cascade = SelectorCascade("x", ["div[style*='{color}']"])
        assert cascade.format(iso="2026-01-01").candidates == ["div[style*='{color}']"]

    def test_combined_joins_candidates(self, registry: SelectorRegistry) -> None:
        combined = registry.get("nuitee.hotel_card").combined
        assert combined.count(",") == len(registry.get("nuitee.hotel_card")) - 1

    def test_strings_returns_plain_markers(self, registry: SelectorRegistry) -> None:
        labels = registry.strings("nuitee.tunnel.summary.total_row_labels")
        assert "total price" in labels
        assert "prix total" in labels   # bilingual coverage

    def test_payment_detectors_present(self, registry: SelectorRegistry) -> None:
        """The payment guard is useless without detectors."""
        assert len(registry.get("nuitee.tunnel.payment.detect")) >= 3

    def test_hit_report_starts_empty(self, registry: SelectorRegistry) -> None:
        assert isinstance(registry.hit_report(), dict)


class TestPaymentGuardConfig:
    def test_guard_tokens_are_lowercased(self) -> None:
        tokens = get_settings().tunnel.payment_guard_tokens
        assert tokens == [t.lower() for t in tokens]

    def test_guard_covers_english_and_french(self) -> None:
        tokens = " ".join(get_settings().tunnel.payment_guard_tokens)
        assert "pay now" in tokens
        assert "payer maintenant" in tokens


class TestVerifierRowSelection:
    @pytest.mark.parametrize(
        "row",
        [
            {"nuitee_final_price": None, "tunnel_status": TunnelStatus.OK.value},
            {"nuitee_final_price": 0, "tunnel_status": TunnelStatus.OK.value},
            {"nuitee_final_price": 900.0,
             "tunnel_status": TunnelStatus.TUNNEL_BLOCKED.value},
            {"nuitee_final_price": 900.0,
             "tunnel_status": TunnelStatus.PAYMENT_GUARD.value},
        ],
    )
    def test_tunnel_retry_detected(self, row: dict) -> None:
        assert needs_tunnel_retry(row) is True

    def test_complete_tunnel_row_is_skipped(self) -> None:
        assert needs_tunnel_retry(
            {"nuitee_final_price": 1200.0, "tunnel_status": TunnelStatus.OK.value}
        ) is False

    @pytest.mark.parametrize(
        "row",
        [
            {"booking_price": None, "match_status": MatchStatus.OK.value},
            {"booking_price": 1200.0, "match_status": MatchStatus.MATCH_FAIL.value},
            {"booking_price": 1200.0, "match_status": MatchStatus.SKIPPED.value},
        ],
    )
    def test_booking_retry_detected(self, row: dict) -> None:
        assert needs_booking_retry(row) is True

    def test_complete_booking_row_is_skipped(self) -> None:
        assert needs_booking_retry(
            {"booking_price": 1200.0, "match_status": MatchStatus.OK.value}
        ) is False

    @pytest.mark.parametrize("mode", ["all", "tunnel", "booking", "unclean"])
    def test_where_clause_is_parameter_safe(self, mode: str) -> None:
        clause, params = build_where_clause(mode)
        assert clause
        assert clause.count("?") == len(params)


class TestCli:
    def test_parser_builds(self) -> None:
        from src.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(
            ["run", "--only-offset", "long", "--durations", "3,10"]
        )
        assert args.command == "run"
        assert args.only_offset == "long"
        assert args.durations == "3,10"

    @pytest.mark.parametrize(
        "value,expected",
        [("short", "short"), ("long", "long"), ("all", None), ("", None)],
    )
    def test_only_offset_parsing(self, value: str, expected) -> None:
        from src.cli import _parse_only_offset

        assert _parse_only_offset(value) == expected

    @pytest.mark.parametrize(
        "value,expected",
        [("3", [3]), ("3,10", [3, 10]), (None, None), ("", None)],
    )
    def test_durations_parsing(self, value, expected) -> None:
        from src.cli import _parse_durations

        assert _parse_durations(value) == expected

    def test_legacy_entrypoint_defaults_to_run(self, monkeypatch) -> None:
        """`python src/main.py --only-offset short` must still work."""
        import src.main as main_module

        captured = {}
        monkeypatch.setattr(main_module, "cli_main", lambda argv: captured.update(argv=argv))
        monkeypatch.setattr("sys.argv", ["main.py", "--only-offset", "short"])
        main_module.main()
        assert captured["argv"] == ["run", "--only-offset", "short"]

    def test_explicit_subcommand_preserved(self, monkeypatch) -> None:
        import src.main as main_module

        captured = {}
        monkeypatch.setattr(main_module, "cli_main", lambda argv: captured.update(argv=argv))
        monkeypatch.setattr("sys.argv", ["main.py", "doctor"])
        main_module.main()
        assert captured["argv"] == ["doctor"]


class TestInputFormats:
    def test_new_format_parsed(self, tmp_path: Path) -> None:
        from src.cli import _load_cities

        path = tmp_path / "input.json"
        path.write_text(
            json.dumps({"version": 2, "rooms_config": [2],
                        "cities": ["Casablanca", "Rabat"]}),
            encoding="utf-8",
        )
        args = type("A", (), {"input": path, "cities": None})()
        entries = _load_cities(args)
        assert [e["city"] for e in entries] == ["Casablanca", "Rabat"]
        assert entries[0]["rooms_config"] == [2]

    def test_legacy_format_parsed(self, tmp_path: Path) -> None:
        from src.cli import _load_cities

        path = tmp_path / "legacy.json"
        path.write_text(
            json.dumps([{"city": "Paris", "checkin": "2026-08-01",
                         "checkout": "2026-08-02", "adults": 2}]),
            encoding="utf-8",
        )
        args = type("A", (), {"input": path, "cities": None})()
        assert _load_cities(args)[0]["city"] == "Paris"

    def test_priority_cities_flagged(self, tmp_path: Path) -> None:
        from src.cli import _load_cities

        path = tmp_path / "input.json"
        path.write_text(
            json.dumps({"version": 2, "rooms_config": [2],
                        "priority_cities": ["Paris"],
                        "cities": ["Paris", "Casablanca"]}),
            encoding="utf-8",
        )
        args = type("A", (), {"input": path, "cities": None})()
        entries = _load_cities(args)
        assert entries[0].get("priority") is True
        assert "priority" not in entries[1]

    def test_cities_flag_overrides_file(self, tmp_path: Path) -> None:
        from src.cli import _load_cities

        args = type("A", (), {"input": tmp_path / "nope.json",
                              "cities": "Fes, Agadir"})()
        assert [e["city"] for e in _load_cities(args)] == ["Fes", "Agadir"]

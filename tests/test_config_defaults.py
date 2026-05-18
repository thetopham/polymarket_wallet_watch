from pathlib import Path

from polymarket_wallet_watch.config import load_config


def test_example_config_tracks_individual_focus_and_two_wallet_convergence():
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.example.yaml").data

    assert cfg["convergence"]["min_wallets"] == 2
    assert cfg["follow_wallet"]["focus_wallet"] == "0xce25e214d5cfe4f459cf67f08df581885aae7fdc"
    assert cfg["follow_wallet"]["enabled"] is True

from pathlib import Path


def test_service_lets_the_supervisor_stop_children_before_forcing_them_down():
    service_file = Path(__file__).parents[1] / "systemd" / "subot.service"

    assert "KillMode=mixed" in service_file.read_text()

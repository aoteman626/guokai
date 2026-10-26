import ouchn_assistant
import pytest

from ouchn_assistant.cli import main


def test_package_version() -> None:
    assert ouchn_assistant.__version__ == "0.1.0"


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--version"])

    assert error.value.code == 0
    assert capsys.readouterr().out.strip() == "0.1.0"

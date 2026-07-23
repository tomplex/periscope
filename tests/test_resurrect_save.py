"""continuum save trigger — periscope drives it because control-mode clients
never expand the status line that continuum's save rides on."""
from pathlib import Path

from periscope import config, resurrect


def test_save_now_is_prod_only(mocker):
    # A stray tick in dev/tests must never write the user's real save file.
    mocker.patch.object(config, "is_prod", return_value=False)
    run = mocker.patch("periscope.resurrect.subprocess.run")
    resurrect.save_now()
    run.assert_not_called()


def test_save_now_degrades_silently_when_continuum_absent(mocker):
    mocker.patch.object(config, "is_prod", return_value=True)
    mocker.patch.object(Path, "exists", return_value=False)
    run = mocker.patch("periscope.resurrect.subprocess.run")
    resurrect.save_now()          # must not raise
    run.assert_not_called()


def test_save_now_invokes_continuum_script(mocker):
    mocker.patch.object(config, "is_prod", return_value=True)
    mocker.patch.object(Path, "exists", return_value=True)
    run = mocker.patch("periscope.resurrect.subprocess.run")
    resurrect.save_now()
    run.assert_called_once()
    assert "continuum_save.sh" in run.call_args.args[0][0]

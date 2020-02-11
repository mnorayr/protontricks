import logging
import os
import shlex
import shutil
import tempfile
from pathlib import Path
from subprocess import check_output, run

__all__ = ("run_command",)

logger = logging.getLogger("protontricks")


def create_wine_bin_dir(steam_runtime_path, proton_app):
    """
    Create a directory with "proxy" executables that load shared libraries
    using Steam Runtime and Proton's own libraries instead of the system
    libraries
    """
    def get_runtime_library_path(steam_runtime_path, proton_app):
        """
        Get LD_LIBRARY_PATH value to run a command using Steam Runtime
        """
        steam_runtime_paths = check_output([
            os.path.join(steam_runtime_path, "run.sh"),
            "--print-steam-runtime-library-paths"
        ])
        steam_runtime_paths = str(steam_runtime_paths, "utf-8")
        # Add Proton installation directory first into LD_LIBRARY_PATH
        # so that libwine.so.1 is picked up correctly (see issue #3)
        return "".join([
            os.path.join(proton_app.install_path, "dist", "lib"), os.pathsep,
            os.path.join(proton_app.install_path, "dist", "lib64"), os.pathsep,
            steam_runtime_paths
        ])

    ld_library_path = get_runtime_library_path(steam_runtime_path, proton_app)

    TEMPLATE = (
        "#!/bin/bash\n"
        "# Helper script created by Protontricks to run Wine binaries using Steam Runtime\n"
        "export LD_LIBRARY_PATH={ld_library_path}\n"
        "exec {path} $@"
    )

    binaries = list((
        Path(proton_app.install_path) / "dist" / "bin"
    ).iterdir())

    # Create a temporary directory to hold the new executables
    # TODO: Maybe use a little more permanent location for these such
    # as the XDG runtime directory?
    bin_path = Path(tempfile.mkdtemp(prefix="protontricks_wine_"))
    logger.info(
        "Created temporary Wine binary directory at %s", str(bin_path)
    )

    for binary in binaries:
        content = TEMPLATE.format(
            ld_library_path=shlex.quote(ld_library_path),
            path=shlex.quote(str(binary))
        ).encode("utf-8")

        with open(bin_path / binary.name, "wb") as file_:
            file_.write(content)

        (bin_path / binary.name).chmod(0o700)

    return bin_path


def run_command(
        steam_path, winetricks_path, proton_app, steam_app, command,
        steam_runtime_path=None,
        **kwargs):
    """Run an arbitrary command with the correct environment variables
    for the given Proton app

    The environment variables are set for the duration of the call
    and restored afterwards

    If 'steam_runtime_path' is provided, run the command using Steam Runtime
    """
    # Make a copy of the environment variables to restore later
    environ_copy = os.environ.copy()

    if not os.environ.get("WINE"):
        logger.info(
            "WINE environment variable is not available. "
            "Setting WINE environment variable to Proton bundled version"
        )
        os.environ["WINE"] = os.path.join(
            proton_app.install_path, "dist", "bin", "wine")

    if not os.environ.get("WINESERVER"):
        logger.info(
            "WINESERVER environment variable is not available. "
            "Setting WINESERVER environment variable to Proton bundled version"
        )
        os.environ["WINESERVER"] = os.path.join(
            proton_app.install_path, "dist", "bin", "wineserver"
        )

    os.environ["WINETRICKS"] = winetricks_path
    os.environ["WINEPREFIX"] = steam_app.prefix_path
    os.environ["WINELOADER"] = os.environ["WINE"]
    os.environ["WINEDLLPATH"] = "".join([
        os.path.join(proton_app.install_path, "dist", "lib64", "wine"),
        os.pathsep,
        os.path.join(proton_app.install_path, "dist", "lib", "wine")
    ])

    os.environ["PATH"] = os.path.join(
        proton_app.install_path, "dist", "bin"
    ) + os.pathsep + os.environ["PATH"]

    # Unset WINEARCH, which might be set for another Wine installation
    os.environ.pop("WINEARCH", "")

    wine_bin_dir = None
    if steam_runtime_path:
        # When Steam Runtime is enabled, create a set of helper scripts
        # that load the underlying Proton Wine executables with Steam Runtime
        # and Proton libraries instead of system libraries
        wine_bin_dir = create_wine_bin_dir(
            steam_runtime_path=steam_runtime_path,
            proton_app=proton_app
        )
        os.environ["PATH"] = str(wine_bin_dir) + os.pathsep + os.environ["PATH"]

    logger.info("Attempting to run command %s", command)

    try:
        run(command, **kwargs)
    finally:
        # Restore original env vars
        os.environ.clear()
        os.environ.update(environ_copy)

        if wine_bin_dir:
            shutil.rmtree(str(wine_bin_dir))

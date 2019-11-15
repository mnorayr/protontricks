import binascii
import glob
import logging
import os
import re
import string
import struct
import zlib

from pathlib import Path

import vdf

__all__ = (
    "COMMON_STEAM_DIRS", "SteamApp", "find_steam_path",
    "find_steam_proton_app", "find_proton_app", "find_steam_runtime_path",
    "find_appid_proton_prefix", "get_steam_lib_paths", "get_steam_apps",
    "get_custom_proton_installations"
)

COMMON_STEAM_DIRS = [
    os.path.join(".steam", "steam"),
    os.path.join(".local", "share", "Steam")
]

logger = logging.getLogger("protontricks")


class SteamApp(object):
    """
    SteamApp represents an installed Steam app or whatever is close enough to
    one (eg. a custom Proton installation or a Windows shortcut with its own
    Proton prefix)
    """
    __slots__ = ("appid", "name", "prefix_path", "install_path")

    def __init__(self, name, install_path, prefix_path=None, appid=None):
        """
        :appid: App's appid
        :name: The app's human-readable name
        :prefix_path: Absolute path to where the app's Wine prefix *might*
                      exist.
        :app_path: Absolute path to app's installation directory
        """
        self.appid = int(appid) if appid else None
        self.name = name
        self.prefix_path = prefix_path
        self.install_path = install_path

    @property
    def prefix_path_exists(self):
        """
        Returns True if the app has a Wine prefix directory that has been
        launched at least once
        """
        if not self.prefix_path:
            return False

        # 'pfx' directory is incomplete until the game has been launched
        # once, so check for 'pfx.lock' as well
        return (
            os.path.exists(self.prefix_path)
            and os.path.exists(os.path.join(self.prefix_path, "..", "pfx.lock"))
        )

    def name_contains(self, s):
        """
        Returns True if the name contains the given substring.
        Both strings are normalized for easier searching before comparison.
        """
        def normalize_str(s):
            """
            Normalize the string to make it easier for human to
            perform a search by removing all symbols
            except ASCII digits and letters and turning it into lowercase
            """
            printable = set(string.printable) - set(string.punctuation)
            s = "".join([c for c in s if c in printable])
            s = s.lower()
            s = s.replace(" ", "")
            return s

        return normalize_str(s) in normalize_str(self.name)

    @property
    def is_proton(self):
        """
        Return True if this app is a Proton installation
        """
        # If the installation directory contains a file named "proton",
        # it's a Proton installation
        return os.path.exists(os.path.join(self.install_path, "proton"))

    @classmethod
    def from_appmanifest(cls, path, steam_lib_paths):
        """
        Parse appmanifest_X.acf file containing Steam app installation metadata
        and return a SteamApp object
        """
        with open(path, "r") as f:
            try:
                content = f.read()
            except UnicodeDecodeError:
                # This might occur if the appmanifest becomes corrupted
                # eg. due to running a Linux filesystem under Windows
                # In that case just skip it
                logger.warning(
                    "Skipping malformed appmanifest {}".format(path)
                )
                return None

        try:
            vdf_data = vdf.loads(content)
        except SyntaxError:
            logger.warning("Skipping malformed appmanifest {}".format(path))
            return None

        try:
            app_state = vdf_data["AppState"]
        except KeyError:
            # Some appmanifest files may be empty. Ignore those.
            logger.info("Skipping empty appmanifest {}".format(path))
            return None

        # The app ID field can be named 'appID' or 'appid'.
        # 'appid' is more common, but certain appmanifest
        # files (created by old Steam clients?) also use 'appID'.
        #
        # Use case-insensitive field names to deal with these.
        app_state = {k.lower(): v for k, v in app_state.items()}
        appid = int(app_state["appid"])
        name = app_state["name"]

        # Proton prefix may exist on a different library
        prefix_path = find_appid_proton_prefix(
            appid=appid, steam_lib_paths=steam_lib_paths
        )

        install_path = os.path.join(
            os.path.split(path)[0], "common", app_state["installdir"])

        return cls(
            appid=appid, name=name, prefix_path=prefix_path,
            install_path=install_path)


def find_steam_path():
    """
    Try to discover default Steam dir using common locations and return the
    first one that matches

    Return (steam_path, steam_root), where steam_path points to
    "~/.steam/steam" (contains "appcache", "config" and "steamapps")
    and "~/.steam/root" (contains "ubuntu12_32" and "compatibilitytools.d")
    """
    def has_steamapps_dir(path):
        """
        Return True if the path either has a 'steamapps' or a 'SteamApps'
        subdirectory, False otherwise
        """
        return (
            # 'steamapps' is the usual name under Linux Steam installations
            os.path.isdir(os.path.join(path, "steamapps"))
            # 'SteamApps' name appears in installations imported from Windows
            or os.path.isdir(os.path.join(path, "SteamApps"))
        )

    def has_runtime_dir(path):
        return os.path.isdir(os.path.join(path, "ubuntu12_32"))

    # as far as @admalledd can tell,
    # this should always be correct for the tools root:
    steam_root = os.path.join(os.path.expanduser("~"), ".steam", "root")

    if not os.path.isdir(os.path.join(steam_root, "ubuntu12_32")):
        # Check that runtime dir exists, if not make root=path and hope
        steam_root = None

    if os.environ.get("STEAM_DIR"):
        steam_path = os.environ.get("STEAM_DIR")
        if has_steamapps_dir(steam_path) and has_runtime_dir(steam_path):
            logger.info(
                "Found a valid Steam installation at %s.", steam_path
            )

            return steam_path, steam_path

        logger.error(
            "$STEAM_DIR was provided but didn't point to a valid Steam "
            "installation."
        )

        return None, None

    for steam_path in COMMON_STEAM_DIRS:
        # The common Steam directories are found inside the home directory
        steam_path = Path.home() / steam_path
        if has_steamapps_dir(steam_path):
            logger.info(
                "Found Steam directory at {}. You can also define Steam "
                "directory manually using $STEAM_DIR".format(steam_path)
            )
            if not steam_root:
                steam_root = steam_path
            return steam_path, steam_root

    return None, None


def find_steam_runtime_path(steam_root):
    """
    Find the Steam Runtime either using the STEAM_RUNTIME env or
    steam_root
    """
    env_steam_runtime = os.environ.get("STEAM_RUNTIME", "")

    if env_steam_runtime == "0":
        # User has disabled Steam Runtime
        logger.info("STEAM_RUNTIME is 0. Disabling Steam Runtime.")
        return None
    elif os.path.isdir(env_steam_runtime):
        # User has a custom Steam Runtime
        logger.info(
            "Using custom Steam Runtime at %s", env_steam_runtime)
        return env_steam_runtime
    elif env_steam_runtime in ["1", ""]:
        # User has enabled Steam Runtime or doesn't have STEAM_RUNTIME set;
        # default to enabled Steam Runtime in either case
        steam_runtime_path = os.path.join(
            steam_root, "ubuntu12_32", "steam-runtime")

        logger.info(
            "Using default Steam Runtime at %s", steam_runtime_path)
        return steam_runtime_path

    logger.error(
        "Path in STEAM_RUNTIME doesn't point to a valid Steam Runtime!")

    return None


APPINFO_STRUCT_HEADER = "<4sL"
APPINFO_STRUCT_SECTION = "<LLLLQ20sL"


def get_appinfo_sections(path):
    """
    Parse an appinfo.vdf file and return all the deserialized binary VDF
    objects inside it
    """
    # appinfo.vdf is not actually a (binary) VDF file, but a binary file
    # containing multiple binary VDF sections.
    # File structure based on comment from vdf developer:
    # https://github.com/ValvePython/vdf/issues/13#issuecomment-321700244
    with open(path, "rb") as f:
        data = f.read()
        i = 0

        # Parse the header
        header_size = struct.calcsize(APPINFO_STRUCT_HEADER)
        magic, universe = struct.unpack(
            APPINFO_STRUCT_HEADER, data[0:header_size]
        )

        i += header_size

        if magic != b"'DV\x07":
            raise SyntaxError("Invalid file magic number")

        sections = []

        section_size = struct.calcsize(APPINFO_STRUCT_SECTION)
        while True:
            # We don't need any of the fields besides 'entry_size',
            # which is used to determine the length of the variable-length VDF
            # field.
            # Still, here they are for posterity's sake.
            (appid, entry_size, infostate, last_updated, access_token,
             sha_hash, change_number) = struct.unpack(
                APPINFO_STRUCT_SECTION, data[i:i+section_size])
            vdf_section_size = entry_size - 40

            i += section_size
            try:
                vdf_d = vdf.binary_loads(data[i:i+vdf_section_size])
                sections.append(vdf_d)
            except UnicodeDecodeError:
                # vdf is unable to decode binary VDF objects containing
                # invalid UTF-8 strings.
                # Since we're only interested in the SteamPlay manifests,
                # we can skip those faulty sections.
                #
                # TODO: Remove this once the upstream bug at
                # https://github.com/ValvePython/vdf/issues/20
                # is fixed
                pass

            i += vdf_section_size

            if i == len(data) - 4:
                return sections


def get_proton_appid(compat_tool_name, appinfo_path):
    """
    Get the App ID for Proton installation by the compat tool name
    used in STEAM_DIR/config/config.vdf
    """
    # Parse all the individual VDF sections in appinfo.vdf to a list
    vdf_sections = get_appinfo_sections(appinfo_path)

    for section in vdf_sections:
        if not section.get("appinfo", {}).get("extended", {}).get(
                "compat_tools", None):
            continue

        compat_tools = section["appinfo"]["extended"]["compat_tools"]

        for default_name, entry in compat_tools.items():
            # A single compatibility tool may have multiple valid names
            # eg. "proton_316" and "proton_316_beta"
            aliases = [default_name]

            # Each compat tool entry can also contain an 'aliases' field
            # with a different compat tool name
            if "aliases" in entry:
                # All of the appinfo.vdf files encountered so far
                # only have a single string inside the "aliases" field,
                # but let's assume the field could be a list of strings
                # as well
                if isinstance(entry["aliases"], str):
                    aliases.append(entry["aliases"])
                elif isinstance(entry["aliases"], list):
                    aliases += entry["aliases"]
                else:
                    raise TypeError(
                        "Unexpected type {} for 'fields' in "
                        "appinfo.vdf".format(type(aliases))
                    )

            if compat_tool_name in aliases:
                return entry["appid"]

    logger.error("Could not find the Steam Play manifest in appinfo.vdf")

    return None


def find_steam_proton_app(steam_path, steam_apps, appid=None):
    """
    Get the current Proton installation used by Steam
    and return a SteamApp object

    If 'appid' is provided, try to find the app-specific Proton installation
    if one is configured
    """
    # 1. Find the name of Proton installation in use
    #    from STEAM_DIR/config/config.vdf
    # 2. If the Proton installation's name can be found directly
    #    in the list of apps we discovered earlier, return that
    # 3. ...or if the name can't be found that way, parse
    #    the file in STEAM_DIR/appcache/appinfo.vdf to find the Proton
    #    installation's App ID
    config_vdf_path = os.path.join(steam_path, "config", "config.vdf")

    with open(config_vdf_path, "r") as f:
        content = f.read()

    vdf_data = vdf.loads(content)
    # ToolMapping seems to be used in older Steam beta releases
    try:
        tool_mapping = (
            vdf_data["InstallConfigStore"]["Software"]["Valve"]["Steam"]
                    ["ToolMapping"]
        )
    except KeyError:
        tool_mapping = {}

    # CompatToolMapping seems to be the name used in newer Steam releases
    # We'll prioritize this if it exists
    try:
        compat_tool_mapping = (
            vdf_data["InstallConfigStore"]["Software"]["Valve"]["Steam"]
                    ["CompatToolMapping"]
        )
    except KeyError:
        compat_tool_mapping = {}

    compat_tool_name = None

    # The name of potential names in order of priority
    potential_names = [
        compat_tool_mapping.get(str(appid), {}).get("name", None),
        compat_tool_mapping.get("0", {}).get("name", None),
        tool_mapping.get(str(appid), {}).get("name", None),
        tool_mapping.get("0", {}).get("name", None)
    ]
    # Get the first name that was valid
    try:
        compat_tool_name = next(name for name in potential_names if name)
    except StopIteration:
        logger.error("No Proton installation found in config.vdf")
        return None

    # We've got the name from config.vdf,
    # now there are two possible ways to find the installation
    # 1. It's a custom Proton installation, and we simply need to find
    #    a SteamApp by its internal name
    # 2. It's a production Proton installation, in which case we need
    #    to parse a binary configuration file to find the App ID

    # Let's try option 1 first
    try:
        app = next(app for app in steam_apps if app.name == compat_tool_name)
        logger.info(
            "Found active custom Proton installation: {}".format(app.name)
        )
        return app
    except StopIteration:
        pass

    # Try option 2:
    # Find the corresponding App ID from <steam_path>/appcache/appinfo.vdf
    appinfo_path = os.path.join(steam_path, "appcache", "appinfo.vdf")
    proton_appid = get_proton_appid(compat_tool_name, appinfo_path)

    if not proton_appid:
        logger.error("Could not find Proton's App ID from appinfo.vdf")
        return None

    # We've now got the appid. Return the corresponding SteamApp
    try:
        app = next(app for app in steam_apps if app.appid == proton_appid)
        logger.info(
            "Found active Proton installation: {}".format(app.name)
        )
        return app
    except StopIteration:
        return None


def find_appid_proton_prefix(appid, steam_lib_paths):
    """
    Find the Proton prefix for the app by its App ID

    Proton prefix and the game installation itself can exist on different
    Steam libraries, making a search necessary
    """
    for path in steam_lib_paths:
        # 'steamapps' portion of the path can also be 'SteamApps'
        for steamapps_part in ("steamapps", "SteamApps"):
            prefix_path = os.path.join(
                path, steamapps_part, "compatdata", str(appid), "pfx"
            )
            if os.path.isdir(prefix_path):
                return prefix_path

    return None


def find_proton_app(steam_path, steam_apps, appid=None):
    """
    Find the Proton app, using either $PROTON_VERSION or the one
    currently configured in Steam

    If 'appid' is provided, use it to find the app-specific Proton installation
    if one is configured
    """
    if os.environ.get("PROTON_VERSION"):
        proton_version = os.environ.get("PROTON_VERSION")
        try:
            proton_app = next(
                app for app in steam_apps if app.name == proton_version)
            logger.info(
                 "Found requested Proton version: {}".format(proton_app.name)
            )
            return proton_app
        except StopIteration:
            logger.error(
                "$PROTON_VERSION was set but matching Proton installation "
                "could not be found."
            )
            return None

    proton_app = find_steam_proton_app(
        steam_path=steam_path, steam_apps=steam_apps, appid=appid)

    if not proton_app:
        logger.error(
            "Active Proton installation could not be found automatically."
        )

    return proton_app


def get_steam_lib_paths(steam_path):
    """
    Return a list of any Steam directories including any user-added
    Steam library folders
    """
    def parse_library_folders(data):
        """
        Parse the Steam library folders in the VDF file using the given data
        """
        vdf_data = vdf.loads(data)
        # Library folders have integer field names in ascending order
        library_folders = [
            value for key, value in vdf_data["LibraryFolders"].items()
            if key.isdigit()
        ]

        logger.info(
            "Found {} Steam library folders".format(len(library_folders))
        )
        logger.info("Steam library folders: %s", library_folders)
        return library_folders

    # Try finding Steam library folders using libraryfolders.vdf in Steam root
    if os.path.isdir(os.path.join(steam_path, "steamapps")):
        folders_vdf_path = os.path.join(
            steam_path, "steamapps", "libraryfolders.vdf")
    elif os.path.isdir(os.path.join(steam_path, "SteamApps")):
        folders_vdf_path = os.path.join(
            steam_path, "SteamApps", "libraryfolders.vdf")

    try:
        with open(folders_vdf_path, "r") as f:
            library_folders = parse_library_folders(f.read())
    except OSError:
        # libraryfolders.vdf doesn't exist; maybe no Steam library folders
        # are set?
        library_folders = []

    return [steam_path] + library_folders


def get_custom_proton_installations(steam_root):
    """
    Return a list of custom Proton installations as a list of SteamApp objects
    """
    comp_root = os.path.join(steam_root, "compatibilitytools.d")

    if not os.path.isdir(comp_root):
        return []

    comptool_files = glob.glob(
        os.path.join(comp_root, "*", "compatibilitytool.vdf")
    )
    comptool_files += glob.glob(
        os.path.join(comp_root, "compatibilitytool.vdf")
    )

    custom_proton_apps = []

    for vdf_path in comptool_files:
        with open(vdf_path, "r") as f:
            content = f.read()

        vdf_data = vdf.loads(content)
        internal_name = list(
            vdf_data["compatibilitytools"]["compat_tools"].keys())[0]
        tool_info = vdf_data["compatibilitytools"]["compat_tools"][
            internal_name]

        install_path = tool_info["install_path"]
        from_oslist = tool_info["from_oslist"]
        to_oslist = tool_info["to_oslist"]

        if from_oslist != "windows" or to_oslist != "linux":
            continue

        # Installation path can be relative if the VDF was in
        # 'compatibilitytools.d/'
        # or '.' if the VDF was in 'compatibilitytools.d/TOOL_NAME'
        if install_path == ".":
            install_path = os.path.dirname(vdf_path)
        else:
            install_path = os.path.join(comp_root, install_path)

        custom_proton_apps.append(
            SteamApp(name=internal_name, install_path=install_path)
        )

    return custom_proton_apps


def find_current_steamid3(steam_path):
    def to_steamid3(steamid64):
        """Convert a SteamID64 into the SteamID3 format"""
        return int(steamid64) & 0xffffffff

    loginusers_path = os.path.join(steam_path, "config", "loginusers.vdf")
    try:
        with open(loginusers_path, "r") as f:
            content = f.read()
            vdf_data = vdf.loads(content)
    except IOError:
        return None

    users = [
        {
            "steamid3": to_steamid3(user_id),
            "account_name": user_data["AccountName"],
            "timestamp": user_data.get("Timestamp", 0)
        }
        for user_id, user_data in vdf_data["users"].items()
    ]

    # Return the user with the highest timestamp, as that's likely to be the
    # currently logged-in user
    if users:
        user = max(users, key=lambda u: u["timestamp"])
        logger.info(
            "Currently logged-in Steam user: %s", user["account_name"]
        )
        return user["steamid3"]

    return None


def get_appid_from_shortcut(target, name):
    """
    Get the identifier used for the Proton prefix from a shortcut's
    target and name
    """
    # First, calculate the screenshot ID Steam uses for shortcuts
    data = b"".join([
        target.encode("utf-8"),
        name.encode("utf-8")
    ])
    result = zlib.crc32(data) & 0xffffffff
    result = result | 0x80000000
    result = (result << 32) | 0x02000000

    # Derive the prefix ID from the screenshot ID
    return result >> 32


def get_custom_windows_shortcuts(steam_path):
    """
    Get a list of custom shortcuts for Windows applications as a list
    of SteamApp objects
    """
    # Get the Steam ID3 for the currently logged-in user
    steamid3 = find_current_steamid3(steam_path)

    shortcuts_path = os.path.join(
        steam_path, "userdata", str(steamid3), "config", "shortcuts.vdf"
    )

    try:
        with open(shortcuts_path, "rb") as f:
            content = f.read()
            vdf_data = vdf.binary_loads(content)
    except IOError:
        logger.info(
            "Couldn't find custom shortcuts. Maybe none have been created yet?"
        )
        return []

    steam_apps = []

    for shortcut_id, shortcut_data in vdf_data["shortcuts"].items():
        # The "exe" field can also be "Exe". Account for this by making
        # all field names lowercase
        shortcut_data = {k.lower(): v for k, v in shortcut_data.items()}
        shortcut_id = int(shortcut_id)

        appid = get_appid_from_shortcut(
            target=shortcut_data["exe"], name=shortcut_data["appname"]
        )

        prefix_path = os.path.join(
            steam_path, "steamapps", "compatdata", str(appid), "pfx"
        )
        install_path = shortcut_data["startdir"].strip('"')

        if not os.path.isdir(prefix_path):
            continue

        steam_apps.append(
            SteamApp(
                appid=appid,
                name="Non-Steam shortcut: {}".format(shortcut_data["appname"]),
                prefix_path=prefix_path, install_path=install_path
            )
        )

    logger.info(
        "Found %d Steam shortcuts running under Proton", len(steam_apps)
    )

    return steam_apps


def get_steam_apps(steam_root, steam_path, steam_lib_paths):
    """
    Find all the installed Steam apps and return them as a list of SteamApp
    objects
    """
    steam_apps = []

    for path in steam_lib_paths:
        appmanifest_paths = []
        if os.path.isdir(os.path.join(path, "steamapps")):
            appmanifest_paths = glob.glob(
                os.path.join(path, "steamapps", "appmanifest_*.acf")
            )
        elif os.path.isdir(os.path.join(path, "SteamApps")):
            appmanifest_paths = glob.glob(
                os.path.join(path, "SteamApps", "appmanifest_*.acf")
            )

        for manifest_path in appmanifest_paths:
            logger.info("Checking appmanifest %s", manifest_path)
            steam_app = SteamApp.from_appmanifest(
                manifest_path, steam_lib_paths=steam_lib_paths
            )
            if steam_app:
                logger.info(
                    "Found app %s. Has prefix: %s.",
                    steam_app.name, steam_app.prefix_path_exists)
                steam_apps.append(steam_app)

    # Get the custom Proton installations and non-Steam shortcuts as well
    steam_apps += get_custom_proton_installations(steam_root=steam_root)
    steam_apps += get_custom_windows_shortcuts(steam_path=steam_path)

    # Exclude games that haven't been launched yet
    steam_apps = [
        app for app in steam_apps if app.prefix_path_exists or app.is_proton
    ]

    # Sort the apps by their names
    steam_apps.sort(key=lambda app: app.name)

    return steam_apps

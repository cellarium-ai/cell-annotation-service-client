import re
from importlib.metadata import version


def get_version() -> str:
    """
    Get the version of the application base on git tag.

    :return: The version of the application.
    """
    return version("cellarium-cas")


def _is_release_version(version_string) -> bool:
    """
    Check if the version string is a release version. Release version is a string that has the format of
    "major.minor.patch".

    Reference: https://semver.org/

    :param version_string: The version string to check

    :return: True if the version string is a release version, False otherwise.
    """
    pattern = r"^\d+\.\d+\.\d+$"
    return re.match(pattern, version_string) is not None


def _is_pre_release_version(version_string) -> bool:
    """
    Check if the version string is a pre-release version. Pre-release version is a string that has the format of
    "major.minor.patch-(alpha|beta|rc).patch".

    Reference: https://semver.org/

    :param version_string: The version string to check

    :return: True if the version string is a pre-release version, False otherwise.
    """
    pattern = r"^\d+\.\d+\.\d+-(alpha|beta|rc)\.\d+$"
    return re.match(pattern, version_string) is not None


def get_version_environment() -> str:
    """
    Get the environment based on the version of the application. The environment is considered as "development" if the
    version is a pre-release version, "production" if the version is a release version, and "test" if the version is
    neither a release version nor a pre-release version.

    Exception: If the version is "0.0.1", the environment is considered as "development" because this version is
    assigned in GitHub Actions executed on branches without a tag.

    :return: The environment name of the application.
    """
    app_version = get_version()
    if _is_pre_release_version(app_version) or app_version == "0.0.1":

        return "development"
    elif _is_release_version(app_version):
        return "production"
    else:
        return "test"

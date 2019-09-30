import datetime
import functools
import jose.exceptions
import json
import logging
import os
import sys
import time

from collections import OrderedDict
from contextlib import contextmanager
from future.utils import viewitems
from hashlib import sha256
from jose import jwt
from stat import S_IRWXG, S_IRWXO, S_IRWXU

from .config import DOT_DIR

if sys.version_info[0] >= 3:
    import configparser

    def timestamp(dt):
        return dt.timestamp()
else:
    import ConfigParser as configparser

    # this really only works if it's in UTC
    def timestamp(dt):
        return int(dt.strftime('%s'))


# TODO: move to config
CLOCK_SKEW_ALLOWANCE = 300         # 5 minutes
GROUP_ROLE_MAP_CACHE_TIME = 3600   # 1 hour

logger = logging.getLogger(__name__)

# the cache directory is the same place we store the config
cache_dir = os.path.join(DOT_DIR, "cache")


def _fix_permissions(path, permissions):
    try:
        os.chmod(path, permissions)
        logger.debug("Successfully repaired permissions on: {}".format(path))
        return True
    except OSError:
        logger.debug("Failed to repair permissions on: {}".format(path))
        return False


def _readable_by_others(path, fix=True):
    mode = os.stat(path).st_mode
    readable_by_others = mode & S_IRWXG or mode & S_IRWXO

    if readable_by_others and fix:
        logger.debug("Cached file at {} has invalid permissions. Attempting to fix.".format(path))

        readable_by_others = not _fix_permissions(path, 0o600)

    return readable_by_others


def _requires_safe_cache_dir(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not safe:
            logger.debug("Cache directory at {} has invalid permissions.".format(cache_dir))
        else:
            return func(*args, **kwargs)

    return wrapper


@contextmanager
def _safe_write(path):
    # Try to open the file as 600
    f = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w")
    yield f
    f.close()


@_requires_safe_cache_dir
def read_aws_shared_credentials():
    """
    :return: A ConfigParser object
    """
    # Create a sha256 of the endpoint url, so fix length and remove weird chars
    path = os.path.join(DOT_DIR, "credentials")
    config = configparser.ConfigParser()

    if not os.path.exists(path) or _readable_by_others(path):
        return config

    logger.debug("Trying to read credentials file at: {}".format(path))

    try:
        with open(path, "r") as f:
            if sys.version_info >= (3, 2):
                config.read_file(f)
            else:
                config.readfp(f)
    except (IOError, OSError):
        logger.debug("Unable to read credentials file from: {}".format(path))

    return config


@_requires_safe_cache_dir
def write_aws_shared_credentials(credentials):
    # Create a sha256 of the role arn, so fix length and remove weird chars
    path = os.path.join(DOT_DIR, "credentials")

    # Try to read in the existing credentials
    config = read_aws_shared_credentials()

    # Add all the new credentials to the config object
    for section in credentials.keys():
        if not config.has_section(section):
            config.add_section(section)

        logger.debug("The section is: {}".format(credentials[section]))

        for key, value in viewitems(credentials[section]):
            config.set(section, key, value)

    # Order all the sections alphabetically
    config._sections = OrderedDict(
        sorted(viewitems(config._sections), key=lambda t: t[0])
    )

    try:
        with _safe_write(path) as f:
            config.write(f)

            logger.debug("Successfully wrote AWS shared credentials credentials to: {}".format(path))

            return path
    except (IOError, OSError):
        logger.error("Unable to write AWS shared credentials to: {}".format(path))

        return None


@_requires_safe_cache_dir
def read_group_role_map(url):
    # Create a sha256 of the endpoint url, so fix length and remove weird chars
    path = os.path.join(cache_dir, "rolemap_" + sha256(url.encode("utf-8")).hexdigest())

    if not os.path.exists(path) or _readable_by_others(path):
        return None

    if time.time() - os.path.getmtime(path) > GROUP_ROLE_MAP_CACHE_TIME:  # expired
        return None
    else:
        logger.debug("Using cached role map for {} at: {}".format(url, path))

        try:
            with open(path, "r") as f:
                return json.load(f)
        except (IOError, OSError):
            logger.debug("Unable to read role map from: {}".format(path))
            return None


@_requires_safe_cache_dir
def write_group_role_map(url, role_map):
    # Create a sha256 of the endpoint url, so fix length and remove weird chars
    url = sha256(url.encode("utf-8")).hexdigest()

    path = os.path.join(cache_dir, "rolemap_" + url)

    try:
        with _safe_write(path) as f:
            json.dump(role_map, f, indent=2)
            f.write("\n")

            logger.debug("Successfully wrote role map to: {}".format(path))
    except (IOError, OSError):
        logger.debug("Unable to write role map to: {}".format(path))


@_requires_safe_cache_dir
def read_id_token(issuer, client_id, key=None):
    if issuer is None or client_id is None:
        return None

    # Create a sha256 of the issuer url, so fix length and remove weird chars
    issuer = sha256(issuer.encode("utf-8")).hexdigest()

    path = os.path.join(cache_dir, "id_" + issuer + "_" + client_id)

    if not os.path.exists(path) or _readable_by_others(path):
        return None

    if not _readable_by_others(path):
        try:
            with open(path, "r") as f:
                token = json.load(f)
        except (IOError, OSError):
            logger.debug("Unable to read id token from: {}".format(path))
            return None

        # Try to decode the ID token
        try:
            id_token_dict = jwt.decode(
                token=token["id_token"],
                key=key,
                audience=client_id
            )
        except jose.exceptions.JOSEError:
            return None

        if id_token_dict.get('exp') - time.time() > CLOCK_SKEW_ALLOWANCE:
            logger.debug("Successfully read cached id token at: {}".format(path))
            return token
        else:
            logger.debug("Cached id token has expired: {}".format(path))
            return None
    else:
        logger.error("Error: id token at {} has improper permissions!".format(path))


@_requires_safe_cache_dir
def write_id_token(issuer, client_id, token):
    if issuer is None or client_id is None:
        return None

    # Create a sha256 of the issuer url, so fix length and remove weird chars
    path = os.path.join(cache_dir,
                        "id_" + sha256(issuer.encode("utf-8")).hexdigest() + "_" + client_id)

    try:
        with _safe_write(path) as f:
            if isinstance(token, dict):
                json.dump(token, f, indent=2)
                f.write("\n")
            else:
                f.write(token)

            logger.debug("Successfully wrote token to: {}".format(path))
    except (IOError, OSError):
        logger.debug("Unable to write id token to: {}".format(path))


@_requires_safe_cache_dir
def read_sts_credentials(role_arn):
    # Create a sha256 of the role arn, so fix length and remove weird chars
    path = os.path.join(cache_dir, "stscreds_" + sha256(role_arn.encode("utf-8")).hexdigest())

    if not os.path.exists(path) or _readable_by_others(path):
        return None

    try:
        with open(path, "r") as f:
            sts = json.load(f)

            exp = datetime.datetime.strptime(sts["Expiration"], '%Y-%m-%dT%H:%M:%SZ')

            if timestamp(exp) - time.time() > CLOCK_SKEW_ALLOWANCE:
                logger.debug("Using STS credentials at: {}, expiring in: {}".format(path, timestamp(exp) - time.time()))
                return sts
            else:
                logger.debug(
                    "Cached STS credentials have expired.".format(path))
                return None
    except (IOError, OSError):
        logger.debug("Unable to read STS credentials from: {}".format(path))
        return None


@_requires_safe_cache_dir
def write_sts_credentials(role_arn, sts_creds):
    # Create a sha256 of the role arn, so fix length and remove weird chars
    path = os.path.join(cache_dir, "stscreds_" + sha256(role_arn.encode("utf-8")).hexdigest())

    try:
        with _safe_write(path) as f:
            json.dump(sts_creds, f, indent=2)
            f.write("\n")

            logger.debug("Successfully wrote STS credentials to: {}".format(path))
    except (IOError, OSError):
        logger.debug("Unable to write STS credentials to: {}".format(path))


def verify_dir_permissions(path=DOT_DIR):
    if os.path.exists(path):
        mode = os.stat(path).st_mode

        logger.debug("Directory permissions on {} are: {}".format(path, mode))

        return (
            mode & S_IRWXU == 448   # 7
            and not mode & S_IRWXG  # 0
            and not mode & S_IRWXO  # 0
        )
    # Attempt to create the directory with the right permissions, if it doesn't exist
    else:
        try:
            os.mkdir(path)
        except (IOError, OSError):
            logger.debug("Unable to create directory: {}".format(path))
            return False

        return _fix_permissions(path, 0o700)


# First let's see if the directory
safe = verify_dir_permissions(DOT_DIR) and verify_dir_permissions(cache_dir)

#!/usr/bin/env python
"""
   # Ensure that a datacube user account exists for the current user
   #
   # It can copy credentials stored in .pgpass to connect to a different database
   # Or create a new user account if required.
"""
from __future__ import print_function

import os
import random
import string
import sys
from collections import namedtuple
from textwrap import dedent

import click
import psycopg2
import pwd
import pytest
from boltons.fileutils import atomic_save
from pathlib import Path

OLD_DB_HOST = "130.56.244.105"
CURRENT_DB_HOST = "dea-db.nci.org.au"
PASSWORD_LENGTH = 32

DBCreds = namedtuple("DBCreds", ["host", "port", "database", "username", "password"])

CANNOT_CONNECT_MSG = """
Unable to connect to the Data Cube database (host={}, port={}, db={}, username={})
Please contact an administrator for help.
"""

USER_ALREADY_EXISTS_MSG = """
An account for '{}' already exists in the Data Cube Database, but
we were unable to connect to it. This can happen if you have used the Data
Cube from raijin, and are now trying to access from VDI, or vice-versa.

To fix this problem, please copy your ~/.pgpass file from the system you
initially used to access the Data Cube, onto the current system.
"""

_PWD = pwd.getpwuid(os.geteuid())
CURRENT_USER = _PWD.pw_name
CURRENT_REAL_NAME = _PWD.pw_gecos
CURRENT_HOME_DIR = _PWD.pw_dir


@click.command()
@click.argument("hostname", required=False)
@click.argument("port", type=click.INT, default=6432, required=False)
@click.argument("dbusername", default=CURRENT_USER, required=False)
def main(hostname, port, dbusername):
    """
    Ensure that a user account exists in the specified Data Cube Database

    ~/.pgpass can have <hostname>:<port>:<database>:<username>:<password>
          e.g., *:*:*:<dbusername>:<password>
    """

    if "PBS_JOBID" in os.environ:
        return

    dbcreds = DBCreds(
        host=hostname,
        port=str(port),
        username=dbusername,
        database="datacube",
        password=None,
    )
    pgpass = Path(CURRENT_HOME_DIR) / ".pgpass"

    try:
        new_creds = find_credentials(pgpass, OLD_DB_HOST, dbcreds)
    except CredentialsNotFound:
        print(
            "\nAttempting to create a database account for the db_user ({}).".format(
                dbcreds.username
            )
        )
        new_creds = create_db_account(dbcreds)
        print("Created new database account.")

    # Append new credentials to ~/.pgpass file
    if new_creds:
        print("Migrating {} to the new database server.".format(dbcreds.username))
        # Add new credentials to ~/.pgpass file
        append_credentials(pgpass, new_creds._replace(host="*", port="*"))

    if not can_connect(dbcreds):
        print_stderr(
            CANNOT_CONNECT_MSG.format(
                dbcreds.host, dbcreds.port, dbcreds.database, dbcreds.username
            )
        )


class CredentialsNotFound(Exception):
    """ Empty class for credentials not found exceptions """


def print_stderr(msg):
    """ Print stderr on the terminal """
    print(msg, file=sys.stderr)


def can_connect(dbcreds):
    """ Can we connect to the database defined by these credentials? """
    try:
        conn = psycopg2.connect(
            host=dbcreds.host,
            port=dbcreds.port,
            user=dbcreds.username,
            database=dbcreds.database,
        )
        cur = conn.cursor()
        cur.execute("SELECT 1;")
        return True
    except psycopg2.Error:
        return False


def find_credentials(pgpass, host, dbcreds):
    """ Find the credential from ~/.pgpass file.

        If pgpass file does not exists or If pgpass exists and is empty, then raise 'credentials not found'
        else if credentials match the production database ip, return the production database credentials for migration
        else if new credentials for migration is already appended to the pgpass file, do nothing
        """
    new_creds = None
    if not pgpass.exists() or os.path.getsize(pgpass) == 0:
        # New user, add new credentials to connect to any database
        raise CredentialsNotFound("New user: Add new credentials to .pgpass file")

    with pgpass.open() as src:
        for line in src:
            # Ignore comments and empty lines
            if not line.strip().startswith("#") and line.strip():
                creds = DBCreds(*line.strip().split(":"))
                if creds.host == host and creds.username == dbcreds.username:
                    # Production database credentials exists
                    new_creds = creds._replace(host="*", port="*")
                elif creds.host == "*" and creds.username == dbcreds.username:
                    # Already migrated to new database format, do noting
                    new_creds = None
    return new_creds


def append_credentials(pgpass, dbcreds):
    """ Append credentials to pgpass file """
    try:
        with pgpass.open() as fin:
            data = fin.read()
    except IOError:
        data = ""

    # The permissions on .pgpass must disallow any access to world or group.
    # Hence, chmod 0600 ~/.pgpass. If the permissions are less strict than this, the file will be ignored.
    with atomic_save(str(pgpass.absolute()), file_perms=0o600, text_mode=True) as fout:
        if data:
            fout.write(data)
            if not data.endswith("\n"):
                fout.write("\n")

        fout.write(":".join(dbcreds) + "\n")
        print("\nUpdated DEA Database Password in ~/.pgpass file.")


def create_db_account(dbcreds):
    """ Create AGDC user account on the requested """
    real_name = CURRENT_REAL_NAME if dbcreds.username == CURRENT_USER else ""

    dbcreds = dbcreds._replace(database="*", password=gen_password())
    try:
        with psycopg2.connect(
            host=dbcreds.host,
            port=dbcreds.port,
            user="guest",
            database="guest",
            password="guest",
        ) as conn:
            with conn.cursor() as cur:
                # Create a new user with login role
                cur.execute(
                    "SELECT create_readonly_agdc_user(%s, %s, %s);",
                    (dbcreds.username, dbcreds.password, real_name),
                )
                return dbcreds
    except psycopg2.Error as err:
        if err.pgerror and "already exists" in err.pgerror:
            print_stderr(USER_ALREADY_EXISTS_MSG.format(dbcreds.username))
        else:
            print_stderr(
                "Error creating user account for {}: {}".format(dbcreds.username, err)
            )
        print_stderr(
            "Please contact a datacube administrator to help resolve user account creation."
        )
        raise err


def gen_password(length=20):
    """ Generate a new password to connect to the database """
    char_set = string.ascii_letters + string.digits
    if not hasattr(gen_password, "rng"):
        gen_password.rng = random.SystemRandom()  # Create a static variable
    return "".join([gen_password.rng.choice(char_set) for _ in range(length)])


if __name__ == "__main__":
    main()


#########
# Tests #
#########


def test_no_pgpass(tmpdir):
    # Create a pgpass.txt file in temp folder
    path = tmpdir.join("pgpass.txt")
    path = Path(str(path))

    assert not path.exists()
    new_creds = DBCreds("*", "*", "*", "username", "MYPASS")

    # No pgpass file exists
    with pytest.raises(CredentialsNotFound):
        find_credentials(path, CURRENT_DB_HOST, new_creds)

    append_credentials(path, new_creds)

    assert path.exists()
    with path.open() as src:
        contents = src.read()

    assert contents == ":".join(new_creds) + "\n"


def test_db_host_doesnot_match_productiondb(tmpdir):
    # Production db credentials
    existing_line = f"{CURRENT_DB_HOST}:5432:*:foo_user:asdf"
    pgpass = tmpdir.join("pgpass.txt")
    pgpass.write(existing_line)

    path = Path(str(pgpass))
    hostdbcreds = DBCreds(
        CURRENT_DB_HOST, "1234", "datacube", "foo_user", "foo_password"
    )
    creds = find_credentials(pgpass, CURRENT_DB_HOST, hostdbcreds)

    assert creds is not None
    assert creds.password == "asdf"

    # Use production db credentials with new host and port being glob star
    append_credentials(path, creds._replace(host="*", port="*"))

    with path.open() as src:
        contents = src.read()

    expected = (
        existing_line
        + "\n"
        + existing_line.replace(f"{CURRENT_DB_HOST}:5432", "*:*")
        + "\n"
    )
    assert contents == expected


def test_pgpass_empty(tmpdir):
    pgpass = tmpdir.join("pgpass.txt")
    pgpass.write("")

    path = Path(str(pgpass))

    new_creds = DBCreds("*", "*", "*", "username", "MYPASS")

    assert path.exists()

    # pgpass file exists and is empty
    with pytest.raises(CredentialsNotFound):
        find_credentials(pgpass, "*", new_creds)

    append_credentials(path, new_creds)

    with path.open() as src:
        contents = src.read()
    assert contents == ":".join(new_creds) + "\n"


def test_db_host_matches_productiondb(tmpdir):
    existing_line = f"{CURRENT_DB_HOST}:5432:*:foo_user:asdf"
    pgpass = tmpdir.join("pgpass.txt")
    pgpass.write(existing_line)

    path = Path(str(pgpass))
    creds = DBCreds(CURRENT_DB_HOST, "1234", "*", "foo_user", "asdf")

    newcreds = find_credentials(pgpass, CURRENT_DB_HOST, creds)

    assert newcreds is not None
    assert newcreds.password == "asdf"

    append_credentials(path, newcreds._replace(host="*", port="*"))

    with path.open() as src:
        contents = src.read()

    expected = (
        existing_line
        + "\n"
        + existing_line.replace(f"{CURRENT_DB_HOST}:5432", "*:*")
        + "\n"
    )
    assert contents == expected


def test_against_emptylines_in_pgpass(tmpdir):
    existing_pgpass = dedent(
        f"""

            {CURRENT_DB_HOST}:5432:*:foo_user:asdf

            {CURRENT_DB_HOST}:*:*:foo_user:asdf
            {CURRENT_DB_HOST}.nci.org.au:*:*:foo_user:asdf
            {CURRENT_DB_HOST}.org.au:*:*:foo_user:asdf

            """
    )
    pgpass = tmpdir.join("pgpass.txt")
    pgpass.write(existing_pgpass)

    path = Path(str(pgpass))
    creds = DBCreds(CURRENT_DB_HOST, "1234", "*", "foo_user", "asdf")

    newcreds = find_credentials(pgpass, CURRENT_DB_HOST, creds)

    assert newcreds is not None
    assert newcreds.password == "asdf"

    append_credentials(path, newcreds._replace(host="*", port="*"))

    with path.open() as src:
        contents = src.read()

    expected = existing_pgpass + "*:*:*:foo_user:asdf\n"
    assert contents == expected


def test_against_comment_in_pgpass(tmpdir):
    existing_pgpass = dedent(
        f"""
            # test comment 1 #
            {CURRENT_DB_HOST}:5432:*:foo_user:asdf

            # test comments 2
            {CURRENT_DB_HOST}:*:*:foo_user:asdf

            # 'test comments 3'
            {CURRENT_DB_HOST}.nci.org.au:*:*:foo_user:asdf

            {CURRENT_DB_HOST}.nci.org.au:*:*:foo_user:asdf

            """
    )
    pgpass = tmpdir.join("pgpass.txt")
    pgpass.write(existing_pgpass)

    path = Path(str(pgpass))
    creds = DBCreds(CURRENT_DB_HOST, "1234", "*", "foo_user", "asdf")

    newcreds = find_credentials(pgpass, CURRENT_DB_HOST, creds)

    assert newcreds is not None
    assert newcreds.password == "asdf"

    append_credentials(path, newcreds._replace(host="*", port="*"))

    with path.open() as src:
        contents = src.read()

    expected = existing_pgpass + "*:*:*:foo_user:asdf\n"
    assert contents == expected

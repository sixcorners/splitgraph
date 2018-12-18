import pytest

from splitgraph.connection import get_connection

try:
    from unittest.mock import patch
except ImportError:
    from mock import patch
from psycopg2.sql import Identifier, SQL
from splitgraph.commands import get_log, commit
from splitgraph.exceptions import SplitGraphException
from splitgraph.commands.info import get_image, get_tables_at
from splitgraph.commands.tagging import get_current_head
from splitgraph.splitfile import execute_commands
from splitgraph.splitfile.execution import _combine_hashes
from splitgraph.hooks.splitfile_commands import PluginCommand
from test.splitgraph.conftest import OUTPUT, PG_MNT, load_splitfile


class DummyCommand(PluginCommand):
    def calc_hash(self, repository, args):
        assert repository == OUTPUT
        assert args == ['arg1', '--arg2', 'argument three']

    def execute(self, repository, args):
        assert repository == OUTPUT
        assert args == ['arg1', '--arg2', 'argument three']


class CalcHashTestCommand(PluginCommand):
    def calc_hash(self, repository, args):
        return 'deadbeef' * 8

    def execute(self, repository, args):
        with get_connection().cursor() as cur:
            cur.execute(SQL("DROP TABLE {}").format(Identifier(args[0])))


def test_dummy_command(sg_pg_conn):
    # Basic test to make sure the config gets wired to the splitfile executor and the arguments
    # are passed to it correctly.
    execute_commands(load_splitfile('custom_command_dummy.splitfile'), output=OUTPUT)
    log = get_log(OUTPUT, get_current_head(OUTPUT))

    assert len(log) == 3  # Base 000.., import from test/pg_mount, DUMMY run that created a dupe image
    assert get_tables_at(OUTPUT, log[0]) == get_tables_at(OUTPUT, log[1])
    assert get_image(OUTPUT, log[0]).comment == 'DUMMY arg1 --arg2 "argument three"'

    # Run the command again -- since it returns a random hash every time, it should add yet another image to the base.
    execute_commands(load_splitfile('custom_command_dummy.splitfile'), output=OUTPUT)
    new_log = get_log(OUTPUT, get_current_head(OUTPUT))

    # Two common images -- 0000... and the import
    assert new_log[2] == log[2]
    assert new_log[1] == log[1]

    # However, the DUMMY command created a new image with a random hash
    assert new_log[0] != log[0]


def test_calc_hash_short_circuit(sg_pg_conn):
    # Test that if the hash returned by calc_hash is unchanged, we don't run execute() again
    execute_commands(load_splitfile('custom_command_calc_hash.splitfile'), output=OUTPUT)

    # Run 1: table gets dropped (since the image doesn't exist)
    log = get_log(OUTPUT, get_current_head(OUTPUT))
    assert len(log) == 3  # Base 000.., import from test/pg_mount, drop table fruits
    assert get_tables_at(OUTPUT, log[0]) == []
    # Hash: combination of the previous image hash and the command context (unchanged)
    assert log[0] == _combine_hashes([log[1], "deadbeef" * 8])

    # Run 2: same command context hash, same original image -- no effect
    with patch('test.splitgraph.splitfile.test_custom_commands.CalcHashTestCommand.execute') as cmd:
        execute_commands(load_splitfile('custom_command_calc_hash.splitfile'), output=OUTPUT)
        new_log = get_log(OUTPUT, get_current_head(OUTPUT))
        assert new_log == log
        assert cmd.call_count == 0

    # Run 3: alter test_pg_mount (same command context hash but different image)
    with sg_pg_conn.cursor() as cur:
        cur.execute("""UPDATE "test/pg_mount".fruits SET name = 'banana' where fruit_id = 1""")
    commit(PG_MNT)
    with patch('test.splitgraph.splitfile.test_custom_commands.CalcHashTestCommand.execute') as cmd:
        execute_commands(load_splitfile('custom_command_calc_hash.splitfile'), output=OUTPUT)
        log_3 = get_log(OUTPUT, get_current_head(OUTPUT))

        assert cmd.call_count == 1
        assert len(log_3) == 3

        # Since we patched the execute() out, it won't have run the DROP TABLE command so we don't check for that.
        # However, the sg_meta is still altered.
        assert log_3[0] == _combine_hashes([log_3[1], "deadbeef" * 8])
        # Import from test/pg_mount changed (since the actual repo changed)
        assert log_3[1] != log[1]
        # Base layer (00000...) unchanged
        assert log_3[2] == log[2]


def test_custom_command_errors(sg_pg_conn):
    # Test we raise for undefined commands
    with pytest.raises(SplitGraphException) as e:
        execute_commands(load_splitfile('custom_command_dummy.splitfile').replace('DUMMY', 'NOP'), output=OUTPUT)
    assert "Custom command NOP not found in the config!" in str(e.value)

    # Test we raise for commands that can't be imported
    with pytest.raises(SplitGraphException) as e:
        execute_commands(load_splitfile('custom_command_dummy.splitfile').replace('DUMMY', 'BROKEN1'), output=OUTPUT)
    assert "Error loading custom command BROKEN1" in str(e.value)
    with pytest.raises(SplitGraphException) as e:
        execute_commands(load_splitfile('custom_command_dummy.splitfile').replace('DUMMY', 'BROKEN2'), output=OUTPUT)
    assert "Error loading custom command BROKEN2" in str(e.value)